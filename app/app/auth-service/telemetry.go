package main

// telemetry.go — instrumentação OpenTelemetry do auth-service.
//
// Fluxo:
//   traces   app --OTLP/HTTP--> OTel Collector --> New Relic (APM)
//   métricas app --OTLP/HTTP--> OTel Collector --(exporter prometheus)--> Prometheus --> Grafana
//   logs     stdout em JSON (com trace_id) --> agente do Collector (filelog) --> Loki
//
// Regras de projeto:
//   - Sem OTEL_EXPORTER_OTLP_ENDPOINT o serviço roda normalmente e a telemetria vira no-op
//     (testes unitários e execução local não precisam de Collector). Erro ao montar um
//     sinal desliga só aquele sinal; OTEL_{TRACES,METRICS}_EXPORTER=none desliga um sinal.
//   - Os instrumentos são criados no escopo do pacote a partir do MeterProvider global.
//     O SDK do OTel "delega" esses instrumentos ao provider real assim que
//     setupTelemetry chama otel.SetMeterProvider.
//   - Nomes das métricas HTTP são IDÊNTICOS nos 5 microsserviços (mesmas queries no Grafana).
//     Por isso o otelhttp fica só com os traces: as métricas dele teriam o mesmo nome
//     (http.server.request.duration) com outros labels.
//   - Rotas são normalizadas por allowlist para não explodir a cardinalidade.

import (
	"context"
	"errors"
	"log"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

const (
	defaultServiceName = "auth-service"
	meterName          = "togglemaster/auth-service"
)

var latencyBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10}

// ---------------------------------------------------------------------------
// Setup dos providers
// ---------------------------------------------------------------------------

// setupTelemetry configura traces e métricas (OTLP/HTTP) e os logs em JSON no
// stdout. Retorna a função de shutdown, que faz o flush final. Nunca derruba o serviço.
func setupTelemetry(ctx context.Context) func(context.Context) error {
	// Com o slog padrão trocado, os log.Printf existentes também saem em JSON.
	slog.SetDefault(slog.New(traceHandler{slog.NewJSONHandler(os.Stdout, nil)}))

	noop := func(context.Context) error { return nil }

	if os.Getenv("OTEL_SDK_DISABLED") == "true" || !otlpEndpointConfigured() {
		log.Println("[telemetry] OTEL_EXPORTER_OTLP_ENDPOINT não definido: telemetria OTel desativada")
		return noop
	}

	hostname, _ := os.Hostname()
	// Ordem importa: o que vem depois sobrescreve. WithFromEnv (OTEL_SERVICE_NAME /
	// OTEL_RESOURCE_ATTRIBUTES) fica por último para o manifest K8s ter a palavra final.
	res, err := resource.New(ctx,
		resource.WithAttributes(
			attribute.String("service.name", defaultServiceName),
			attribute.String("service.instance.id", hostname),
		),
		resource.WithTelemetrySDK(),
		resource.WithFromEnv(),
	)
	if err != nil {
		log.Printf("[telemetry] erro ao montar resource (seguindo sem telemetria): %v", err)
		return noop
	}

	// W3C traceparent: é o que liga este serviço ao flag-service e ao
	// targeting-service no trace distribuído.
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{}, propagation.Baggage{}))

	var shutdowns []func(context.Context) error
	ativos := []string{}

	if os.Getenv("OTEL_TRACES_EXPORTER") != "none" {
		if exp, err := otlptracehttp.New(ctx); err != nil {
			log.Printf("[telemetry] erro ao criar exporter de traces (seguindo sem traces): %v", err)
		} else {
			tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exp), sdktrace.WithResource(res))
			otel.SetTracerProvider(tp)
			shutdowns = append(shutdowns, tp.Shutdown)
			ativos = append(ativos, "traces")
		}
	}

	if os.Getenv("OTEL_METRICS_EXPORTER") != "none" {
		if provider, err := newMeterProvider(ctx, res); err != nil {
			log.Printf("[telemetry] erro ao criar exporter de métricas (seguindo sem métricas): %v", err)
		} else {
			otel.SetMeterProvider(provider)
			shutdowns = append(shutdowns, provider.Shutdown)
			ativos = append(ativos, "métricas")

			// Heartbeat: +1 por processo. Permite ver "serviço com telemetria ativa" no Grafana
			// mesmo sem tráfego (a série cumulativa continua sendo exportada).
			if up, err := otel.Meter(meterName).Int64UpDownCounter("app.up",
				metric.WithUnit("{instance}"),
				metric.WithDescription("1 por instância do processo com telemetria ativa"),
			); err == nil {
				up.Add(ctx, 1)
			}
		}
	}

	log.Printf("[telemetry] OTel ativo (service=%s, sinais=%v)", defaultServiceName, ativos)

	return func(ctx context.Context) error {
		var errs error
		for _, fn := range shutdowns {
			errs = errors.Join(errs, fn(ctx))
		}
		return errs
	}
}

func newMeterProvider(ctx context.Context, res *resource.Resource) (*sdkmetric.MeterProvider, error) {
	exporter, err := otlpmetrichttp.New(ctx) // lê OTEL_EXPORTER_OTLP_ENDPOINT (+ /v1/metrics)
	if err != nil {
		return nil, err
	}

	interval := 15 * time.Second
	if v, err := strconv.Atoi(os.Getenv("OTEL_METRIC_EXPORT_INTERVAL")); err == nil && v > 0 {
		interval = time.Duration(v) * time.Millisecond
	}

	return sdkmetric.NewMeterProvider(
		sdkmetric.WithResource(res),
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exporter, sdkmetric.WithInterval(interval))),
	), nil
}

func otlpEndpointConfigured() bool {
	for _, v := range []string{
		"OTEL_EXPORTER_OTLP_ENDPOINT",
		"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
		"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
	} {
		if os.Getenv(v) != "" {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// Traces: servidor HTTP e PostgreSQL
// ---------------------------------------------------------------------------

// tracer é buscado a cada uso (e não guardado numa variável do pacote) para
// sempre seguir o TracerProvider global atual.
func tracer() trace.Tracer {
	return otel.Tracer(defaultServiceName)
}

// newTracedHandler envolve o handler: cada requisição vira um span de servidor e
// o contexto de trace recebido do flag-service/targeting-service (traceparent) é
// continuado. O /health fica de fora para os probes do kubelet não poluírem o APM.
func newTracedHandler(h http.Handler) http.Handler {
	return otelhttp.NewHandler(h, defaultServiceName,
		otelhttp.WithMeterProvider(metricnoop.NewMeterProvider()),
		otelhttp.WithSpanNameFormatter(func(_ string, r *http.Request) string {
			return r.Method + " " + r.URL.Path
		}),
		otelhttp.WithFilter(func(r *http.Request) bool {
			return r.URL.Path != "/health"
		}),
	)
}

// startDBSpan abre o span de cliente de uma query no PostgreSQL. O pgx v4 não tem
// instrumentação oficial do OpenTelemetry, então o span é manual. Com db.system o
// New Relic mostra o banco como dependência do serviço no Service Map.
func startDBSpan(ctx context.Context, operation, table, statement string) (context.Context, trace.Span) {
	return tracer().Start(ctx, operation+" "+table,
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(
			attribute.String("db.system", "postgresql"),
			attribute.String("db.operation", operation),
			attribute.String("db.sql.table", table),
			attribute.String("db.statement", statement), // parametrizada: sem valores
		))
}

// recordDBError marca a falha de banco no span da query e no span da requisição.
// O /validate responde 401 quando o banco cai (comportamento original), e um
// 401 não conta como erro no APM; sem isto a queda do banco ficaria invisível
// na taxa de erro do serviço.
func recordDBError(ctx context.Context, dbSpan trace.Span, err error) {
	dbSpan.RecordError(err)
	dbSpan.SetStatus(codes.Error, "falha no PostgreSQL")
	trace.SpanFromContext(ctx).SetStatus(codes.Error, "falha no PostgreSQL")
}

// ---------------------------------------------------------------------------
// Logs: JSON no stdout com trace_id/span_id
// ---------------------------------------------------------------------------

// traceHandler acrescenta trace_id/span_id em cada linha de log, para pular
// do log no Loki direto para o trace da requisição no APM.
type traceHandler struct{ slog.Handler }

func (h traceHandler) Handle(ctx context.Context, r slog.Record) error {
	if sc := trace.SpanContextFromContext(ctx); sc.IsValid() {
		r.AddAttrs(slog.String("trace_id", sc.TraceID().String()), slog.String("span_id", sc.SpanID().String()))
	}
	return h.Handler.Handle(ctx, r)
}

func (h traceHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	return traceHandler{h.Handler.WithAttrs(attrs)}
}

func (h traceHandler) WithGroup(name string) slog.Handler {
	return traceHandler{h.Handler.WithGroup(name)}
}

// ---------------------------------------------------------------------------
// Métricas HTTP (padrão comum aos 5 serviços)
// ---------------------------------------------------------------------------

type httpMetrics struct {
	requests metric.Int64Counter       // http_server_requests_total
	duration metric.Float64Histogram   // http_server_request_duration_seconds
	active   metric.Int64UpDownCounter // http_server_active_requests
}

func newHTTPMetrics(m metric.Meter) *httpMetrics {
	requests, _ := m.Int64Counter("http.server.requests",
		metric.WithUnit("{request}"),
		metric.WithDescription("Total de requisições HTTP recebidas"))
	duration, _ := m.Float64Histogram("http.server.request.duration",
		metric.WithUnit("s"),
		metric.WithDescription("Duração das requisições HTTP"),
		metric.WithExplicitBucketBoundaries(latencyBuckets...))
	active, _ := m.Int64UpDownCounter("http.server.active_requests",
		metric.WithUnit("{request}"),
		metric.WithDescription("Requisições HTTP em andamento"))
	return &httpMetrics{requests: requests, duration: duration, active: active}
}

var httpm = newHTTPMetrics(otel.Meter(meterName))

// routeLabel devolve o rótulo de rota e se a rota deve ser medida.
// /health fica de fora por padrão (sondas do kubelet poluiriam a taxa de requisições);
// OTEL_METRICS_INCLUDE_HEALTH=true reativa.
func routeLabel(path string) (string, bool) {
	switch path {
	case "/health":
		return path, os.Getenv("OTEL_METRICS_INCLUDE_HEALTH") == "true"
	case "/validate", "/admin/keys":
		return path, true
	default:
		return "unmatched", true // 404s/scans: contados, mas sem cardinalidade
	}
}

func methodLabel(m string) string {
	switch m {
	case http.MethodGet, http.MethodPost, http.MethodPut, http.MethodPatch,
		http.MethodDelete, http.MethodHead, http.MethodOptions:
		return m
	default:
		return "OTHER"
	}
}

type statusRecorder struct {
	http.ResponseWriter
	status      int
	wroteHeader bool
}

func (r *statusRecorder) WriteHeader(code int) {
	if !r.wroteHeader {
		r.status = code
		r.wroteHeader = true
	}
	r.ResponseWriter.WriteHeader(code)
}

func (r *statusRecorder) Write(b []byte) (int, error) {
	if !r.wroteHeader {
		r.wroteHeader = true // status implícito 200
	}
	return r.ResponseWriter.Write(b)
}

func (r *statusRecorder) Unwrap() http.ResponseWriter { return r.ResponseWriter }

// metricsMiddleware mede taxa, erros (via status) e latência de cada requisição.
func metricsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		route, tracked := routeLabel(r.URL.Path)
		if !tracked {
			next.ServeHTTP(w, r)
			return
		}

		ctx := r.Context()
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		httpm.active.Add(ctx, 1)

		finish := func() {
			httpm.active.Add(ctx, -1)
			attrs := metric.WithAttributes(
				attribute.String("method", methodLabel(r.Method)),
				attribute.String("route", route),
				attribute.String("status_code", strconv.Itoa(rec.status)),
				attribute.String("status_class", strconv.Itoa(rec.status/100)+"xx"),
			)
			httpm.requests.Add(ctx, 1, attrs)
			httpm.duration.Record(ctx, time.Since(start).Seconds(), attrs)
		}

		defer func() {
			if p := recover(); p != nil {
				rec.status = http.StatusInternalServerError
				finish()
				panic(p) // o net/http trata (loga e fecha a conexão)
			}
			finish()
		}()

		next.ServeHTTP(rec, r)
	})
}

// ---------------------------------------------------------------------------
// Métricas de negócio do auth-service
// ---------------------------------------------------------------------------

type authMetrics struct {
	validations metric.Int64Counter     // auth_key_validations_total{result}
	keysCreated metric.Int64Counter     // auth_keys_created_total
	dbDuration  metric.Float64Histogram // auth_db_query_duration_seconds{query}
}

func newAuthMetrics(m metric.Meter) *authMetrics {
	validations, _ := m.Int64Counter("auth.key.validations",
		metric.WithUnit("{validation}"),
		metric.WithDescription("Validações de chave de API por resultado (valid|invalid|db_error|missing_header)"))
	keysCreated, _ := m.Int64Counter("auth.keys.created",
		metric.WithUnit("{key}"),
		metric.WithDescription("Chaves de API criadas"))
	dbDuration, _ := m.Float64Histogram("auth.db.query.duration",
		metric.WithUnit("s"),
		metric.WithDescription("Duração das queries no PostgreSQL"),
		metric.WithExplicitBucketBoundaries(latencyBuckets...))
	return &authMetrics{validations: validations, keysCreated: keysCreated, dbDuration: dbDuration}
}

var authm = newAuthMetrics(otel.Meter(meterName))

func (m *authMetrics) validation(result string) {
	m.validations.Add(context.Background(), 1,
		metric.WithAttributes(attribute.String("result", result)))
}

func (m *authMetrics) dbQuery(query string, start time.Time) {
	m.dbDuration.Record(context.Background(), time.Since(start).Seconds(),
		metric.WithAttributes(attribute.String("query", query)))
}
