package main

// telemetry.go — instrumentação OpenTelemetry do evaluation-service.
//
// Tudo sai via OTLP/HTTP para o OTel Collector, que roteia cada sinal:
//
//   traces   app --OTLP--> Collector --> New Relic (APM)
//   métricas app --OTLP--> Collector --(exporter prometheus)--> Prometheus --> Grafana
//   logs     stdout em JSON (com trace_id) --> agente do Collector (filelog) --> Loki
//
// Regras de projeto:
//   - Sem OTEL_EXPORTER_OTLP_ENDPOINT (ou com OTEL_SDK_DISABLED=true) o serviço roda
//     normalmente e a telemetria vira no-op (testes unitários e execução local não
//     precisam de Collector). Erro ao montar um sinal desliga só aquele sinal.
//   - Tracer e instrumentos são criados no escopo do pacote a partir dos providers
//     globais; o SDK os "delega" aos providers reais quando initTelemetry roda.
//   - Nomes das métricas HTTP são IDÊNTICOS nos 5 microsserviços (mesmas queries no
//     Grafana). Por isso o otelhttp fica só com os traces: as métricas dele teriam o
//     mesmo nome (http.server.request.duration) com outros labels.
//   - Rotas são normalizadas por allowlist para não explodir a cardinalidade.
//
// Variáveis OTEL_* padrão: OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_SERVICE_NAME,
// OTEL_RESOURCE_ATTRIBUTES, OTEL_METRIC_EXPORT_INTERVAL (ms) e
// OTEL_{TRACES,METRICS,LOGS}_EXPORTER=none para desligar um sinal. No cluster os
// logs via OTLP ficam desligados: o agente já coleta o stdout de todos os pods.

import (
	"context"
	"errors"
	"log"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"time"

	"go.opentelemetry.io/contrib/bridges/otelslog"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/contrib/instrumentation/runtime"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/log/global"
	"go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/propagation"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

const (
	serviceName = "evaluation-service"
	meterName   = "togglemaster/evaluation-service"
)

var (
	tracer         = otel.Tracer(serviceName)
	latencyBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10}
)

// ---------------------------------------------------------------------------
// Setup dos providers
// ---------------------------------------------------------------------------

// initTelemetry configura traces, métricas e logs e devolve a função que faz o
// flush final no desligamento. Nunca derruba o serviço.
func initTelemetry(ctx context.Context) func(context.Context) error {
	stdout := traceHandler{slog.NewJSONHandler(os.Stdout, nil)}
	// Com o slog padrão trocado, os log.Printf existentes também saem em JSON.
	slog.SetDefault(slog.New(stdout))

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
			attribute.String("service.name", serviceName),
			attribute.String("service.instance.id", hostname),
		),
		resource.WithTelemetrySDK(),
		resource.WithFromEnv(),
	)
	if err != nil {
		log.Printf("[telemetry] erro ao montar resource (seguindo sem telemetria): %v", err)
		return noop
	}

	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{}, propagation.Baggage{}))

	var shutdowns []func(context.Context) error
	ativos := []string{}

	if exporterEnabled("OTEL_TRACES_EXPORTER") {
		if exp, err := otlptracehttp.New(ctx); err != nil {
			log.Printf("[telemetry] erro ao criar exporter de traces (seguindo sem traces): %v", err)
		} else {
			tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exp), sdktrace.WithResource(res))
			otel.SetTracerProvider(tp)
			shutdowns = append(shutdowns, tp.Shutdown)
			ativos = append(ativos, "traces")
		}
	}

	if exporterEnabled("OTEL_METRICS_EXPORTER") {
		if mp, err := newMeterProvider(ctx, res); err != nil {
			log.Printf("[telemetry] erro ao criar exporter de métricas (seguindo sem métricas): %v", err)
		} else {
			otel.SetMeterProvider(mp)
			shutdowns = append(shutdowns, mp.Shutdown)
			ativos = append(ativos, "métricas")

			// Goroutines, heap e GC — o básico para investigar vazamento.
			if err := runtime.Start(runtime.WithMeterProvider(mp)); err != nil {
				log.Printf("[telemetry] métricas de runtime desativadas: %v", err)
			}

			// Heartbeat: +1 por processo. Permite ver "serviço com telemetria ativa" no
			// Grafana mesmo sem tráfego (a série cumulativa continua sendo exportada).
			if up, err := otel.Meter(meterName).Int64UpDownCounter("app.up",
				metric.WithUnit("{instance}"),
				metric.WithDescription("1 por instância do processo com telemetria ativa"),
			); err == nil {
				up.Add(ctx, 1)
			}
		}
	}

	if exporterEnabled("OTEL_LOGS_EXPORTER") {
		if exp, err := otlploghttp.New(ctx); err != nil {
			log.Printf("[telemetry] erro ao criar exporter de logs (seguindo só com stdout): %v", err)
		} else {
			lp := sdklog.NewLoggerProvider(sdklog.WithProcessor(sdklog.NewBatchProcessor(exp)), sdklog.WithResource(res))
			global.SetLoggerProvider(lp)
			shutdowns = append(shutdowns, lp.Shutdown)
			ativos = append(ativos, "logs")
			slog.SetDefault(slog.New(fanoutHandler{stdout, otelslog.NewHandler(serviceName, otelslog.WithLoggerProvider(lp))}))
		}
	}

	log.Printf("[telemetry] OTel ativo (service=%s, sinais=%v)", serviceName, ativos)

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
		"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
	} {
		if os.Getenv(v) != "" {
			return true
		}
	}
	return false
}

func exporterEnabled(envVar string) bool {
	return os.Getenv(envVar) != "none"
}

// ---------------------------------------------------------------------------
// Traces: servidor, cliente HTTP e Service Bus
// ---------------------------------------------------------------------------

// newTracedHandler envolve o handler: cada requisição vira um span de servidor e
// o contexto de trace recebido (traceparent) é continuado. O /health fica de
// fora para os probes do kubelet não poluírem o APM.
func newTracedHandler(h http.Handler) http.Handler {
	return otelhttp.NewHandler(h, serviceName,
		otelhttp.WithMeterProvider(metricnoop.NewMeterProvider()),
		otelhttp.WithSpanNameFormatter(func(_ string, r *http.Request) string {
			return r.Method + " " + r.URL.Path
		}),
		otelhttp.WithFilter(func(r *http.Request) bool {
			return r.URL.Path != "/health"
		}),
	)
}

// newHTTPClient cria o cliente das chamadas ao flag-service e ao targeting-service.
// O transporte instrumentado gera o span de cliente e injeta o traceparent,
// que é o que liga os serviços no trace distribuído e no Service Map. A métrica
// dessas chamadas é a evaluation.upstream.* (evaluator.go).
func newHTTPClient() *http.Client {
	return &http.Client{
		Timeout: 5 * time.Second,
		Transport: otelhttp.NewTransport(http.DefaultTransport,
			otelhttp.WithMeterProvider(metricnoop.NewMeterProvider()),
			otelhttp.WithSpanNameFormatter(func(_ string, r *http.Request) string {
				return r.Method + " " + r.URL.Host
			}),
		),
	}
}

// messageTraceProperties devolve o contexto de trace no formato das
// ApplicationProperties do Service Bus, para o analytics-service continuar o
// mesmo trace ao consumir a mensagem. Diagnostic-Id é a convenção dos SDKs
// Azure; traceparent é a do W3C.
func messageTraceProperties(ctx context.Context) map[string]any {
	carrier := propagation.MapCarrier{}
	otel.GetTextMapPropagator().Inject(ctx, carrier)

	props := make(map[string]any, len(carrier)+1)
	for k, v := range carrier {
		props[k] = v
	}
	if tp, ok := carrier["traceparent"]; ok {
		props["Diagnostic-Id"] = tp
	}
	return props
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

// fanoutHandler manda cada log para stdout e para o OTLP (quando os logs via
// OTLP estão ligados).
type fanoutHandler []slog.Handler

func (f fanoutHandler) Enabled(ctx context.Context, level slog.Level) bool {
	for _, h := range f {
		if h.Enabled(ctx, level) {
			return true
		}
	}
	return false
}

func (f fanoutHandler) Handle(ctx context.Context, r slog.Record) error {
	var errs error
	for _, h := range f {
		if h.Enabled(ctx, r.Level) {
			errs = errors.Join(errs, h.Handle(ctx, r.Clone()))
		}
	}
	return errs
}

func (f fanoutHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	out := make(fanoutHandler, len(f))
	for i, h := range f {
		out[i] = h.WithAttrs(attrs)
	}
	return out
}

func (f fanoutHandler) WithGroup(name string) slog.Handler {
	out := make(fanoutHandler, len(f))
	for i, h := range f {
		out[i] = h.WithGroup(name)
	}
	return out
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
	case "/evaluate":
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
// Métricas de negócio do evaluation-service
// ---------------------------------------------------------------------------

type evalMetrics struct {
	cache            metric.Int64Counter     // evaluation_cache_requests_total{result=hit|miss|error}
	decisions        metric.Int64Counter     // evaluation_decisions_total{result=true|false}
	upstreamRequests metric.Int64Counter     // evaluation_upstream_requests_total{upstream,outcome}
	upstreamDuration metric.Float64Histogram // evaluation_upstream_duration_seconds{upstream}
	events           metric.Int64Counter     // evaluation_events_published_total{outcome}
}

func newEvalMetrics(m metric.Meter) *evalMetrics {
	cache, _ := m.Int64Counter("evaluation.cache.requests",
		metric.WithUnit("{request}"),
		metric.WithDescription("Consultas ao cache Redis por resultado (hit|miss|error)"))
	decisions, _ := m.Int64Counter("evaluation.decisions",
		metric.WithUnit("{decision}"),
		metric.WithDescription("Decisões de avaliação de flag (result=true|false)"))
	upstreamRequests, _ := m.Int64Counter("evaluation.upstream.requests",
		metric.WithUnit("{request}"),
		metric.WithDescription("Chamadas a flag-service/targeting-service por desfecho (ok|not_found|http_error|error)"))
	upstreamDuration, _ := m.Float64Histogram("evaluation.upstream.duration",
		metric.WithUnit("s"),
		metric.WithDescription("Latência das chamadas a flag-service/targeting-service"),
		metric.WithExplicitBucketBoundaries(latencyBuckets...))
	events, _ := m.Int64Counter("evaluation.events.published",
		metric.WithUnit("{event}"),
		metric.WithDescription("Eventos enviados ao Service Bus (success|error|disabled)"))
	return &evalMetrics{
		cache: cache, decisions: decisions,
		upstreamRequests: upstreamRequests, upstreamDuration: upstreamDuration,
		events: events,
	}
}

var evalm = newEvalMetrics(otel.Meter(meterName))

func (m *evalMetrics) cacheResult(result string) {
	m.cache.Add(context.Background(), 1,
		metric.WithAttributes(attribute.String("result", result)))
}

func (m *evalMetrics) decision(result bool) {
	m.decisions.Add(context.Background(), 1,
		metric.WithAttributes(attribute.String("result", strconv.FormatBool(result))))
}

func (m *evalMetrics) upstream(name, outcome string, start time.Time) {
	m.upstreamRequests.Add(context.Background(), 1, metric.WithAttributes(
		attribute.String("upstream", name), attribute.String("outcome", outcome)))
	m.upstreamDuration.Record(context.Background(), time.Since(start).Seconds(),
		metric.WithAttributes(attribute.String("upstream", name)))
}

func (m *evalMetrics) eventPublished(outcome string) {
	m.events.Add(context.Background(), 1,
		metric.WithAttributes(attribute.String("outcome", outcome)))
}
