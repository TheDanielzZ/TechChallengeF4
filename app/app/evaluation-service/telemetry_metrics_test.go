package main

import (
	"bytes"
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

// --- Leitura das métricas em memória ----------------------------------------
//
// Os instrumentos do pacote (httpm, evalm) nascem do MeterProvider global e são
// ligados ao PRIMEIRO provider real configurado. Por isso o leitor em memória é
// instalado uma vez só, antes de qualquer teste que dependa dele.

var (
	metricasOnce   sync.Once
	metricasLeitor *sdkmetric.ManualReader
)

func leitorDeMetricas(t *testing.T) *sdkmetric.ManualReader {
	t.Helper()
	metricasOnce.Do(func() {
		metricasLeitor = sdkmetric.NewManualReader()
		otel.SetMeterProvider(sdkmetric.NewMeterProvider(sdkmetric.WithReader(metricasLeitor)))
	})
	return metricasLeitor
}

// valorMetrica soma os pontos da métrica que têm todos os atributos pedidos.
// Para histogramas, soma a quantidade de observações.
func valorMetrica(t *testing.T, nome string, attrs map[string]string) int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := leitorDeMetricas(t).Collect(context.Background(), &rm); err != nil {
		t.Fatalf("erro ao coletar métricas: %v", err)
	}

	var total int64
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != nome {
				continue
			}
			switch d := m.Data.(type) {
			case metricdata.Sum[int64]:
				for _, p := range d.DataPoints {
					if temAtributos(p.Attributes, attrs) {
						total += p.Value
					}
				}
			case metricdata.Histogram[float64]:
				for _, p := range d.DataPoints {
					if temAtributos(p.Attributes, attrs) {
						total += int64(p.Count)
					}
				}
			}
		}
	}
	return total
}

func temAtributos(set attribute.Set, attrs map[string]string) bool {
	for k, v := range attrs {
		got, ok := set.Value(attribute.Key(k))
		if !ok || got.AsString() != v {
			return false
		}
	}
	return true
}

// --- Rotas e métodos ---------------------------------------------------------

func TestRouteLabel_NormalizaRotas(t *testing.T) {
	casos := []struct {
		path   string
		rota   string
		medida bool
	}{
		{"/evaluate", "/evaluate", true},
		{"/health", "/health", false},
		{"/evaluate/x", "unmatched", true},
		{"/.env", "unmatched", true},
	}
	for _, c := range casos {
		rota, medida := routeLabel(c.path)
		if rota != c.rota || medida != c.medida {
			t.Errorf("routeLabel(%q) = (%q, %v), esperado (%q, %v)", c.path, rota, medida, c.rota, c.medida)
		}
	}
}

func TestRouteLabel_HealthMedidoQuandoPedido(t *testing.T) {
	t.Setenv("OTEL_METRICS_INCLUDE_HEALTH", "true")
	if _, medida := routeLabel("/health"); !medida {
		t.Error("com OTEL_METRICS_INCLUDE_HEALTH=true o /health deveria ser medido")
	}
}

func TestMethodLabel_MetodoDesconhecidoViraOther(t *testing.T) {
	if got := methodLabel(http.MethodGet); got != http.MethodGet {
		t.Errorf("methodLabel(GET) = %q", got)
	}
	if got := methodLabel("BREW"); got != "OTHER" {
		t.Errorf("methodLabel(BREW) = %q, esperado OTHER", got)
	}
}

// --- Middleware HTTP ---------------------------------------------------------

func TestMetricsMiddleware_ContaRequisicaoPorRotaEStatus(t *testing.T) {
	leitorDeMetricas(t)
	attrs := map[string]string{"route": "/evaluate", "status_code": "502", "status_class": "5xx", "method": "GET"}
	antes := valorMetrica(t, "http.server.requests", attrs)
	antesDuracao := valorMetrica(t, "http.server.request.duration", attrs)

	h := metricsMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	}))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/evaluate", nil))

	if got := valorMetrica(t, "http.server.requests", attrs) - antes; got != 1 {
		t.Errorf("http.server.requests{502} aumentou %d, esperado 1", got)
	}
	if got := valorMetrica(t, "http.server.request.duration", attrs) - antesDuracao; got != 1 {
		t.Errorf("http.server.request.duration{502} ganhou %d observações, esperado 1", got)
	}
	if got := valorMetrica(t, "http.server.active_requests", nil); got != 0 {
		t.Errorf("http.server.active_requests = %d depois da requisição, esperado 0", got)
	}
}

func TestMetricsMiddleware_NaoMedeHealth(t *testing.T) {
	leitorDeMetricas(t)
	attrs := map[string]string{"route": "/health"}
	antes := valorMetrica(t, "http.server.requests", attrs)

	chamou := false
	h := metricsMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { chamou = true }))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/health", nil))

	if !chamou {
		t.Fatal("o handler do /health não foi chamado")
	}
	if got := valorMetrica(t, "http.server.requests", attrs) - antes; got != 0 {
		t.Errorf("/health foi contado %d vez(es), esperado 0", got)
	}
}

func TestMetricsMiddleware_PanicContaComo500EPropaga(t *testing.T) {
	leitorDeMetricas(t)
	attrs := map[string]string{"route": "/evaluate", "status_code": "500"}
	antes := valorMetrica(t, "http.server.requests", attrs)

	h := metricsMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		panic("falha simulada")
	}))

	func() {
		defer func() {
			if recover() == nil {
				t.Error("o panic deveria continuar subindo para o net/http")
			}
		}()
		h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/evaluate", nil))
	}()

	if got := valorMetrica(t, "http.server.requests", attrs) - antes; got != 1 {
		t.Errorf("panic contado %d vez(es) como 500, esperado 1", got)
	}
}

func TestStatusRecorder_StatusImplicito200EUnwrap(t *testing.T) {
	inner := httptest.NewRecorder()
	rec := &statusRecorder{ResponseWriter: inner, status: http.StatusOK}

	rec.Write([]byte("ok"))
	rec.WriteHeader(http.StatusInternalServerError) // ignorado: o corpo já saiu com 200

	if rec.status != http.StatusOK {
		t.Errorf("status = %d, esperado 200", rec.status)
	}
	if rec.Unwrap() != inner {
		t.Error("Unwrap deveria devolver o ResponseWriter original")
	}
}

// --- Métricas de negócio -----------------------------------------------------

func TestEvalMetrics_RegistraOsDesfechos(t *testing.T) {
	leitorDeMetricas(t)
	cache := map[string]string{"result": "error"}
	upstream := map[string]string{"upstream": "flag-service", "outcome": "http_error"}
	evento := map[string]string{"outcome": "disabled"}
	decisao := map[string]string{"result": "true"}

	antesCache := valorMetrica(t, "evaluation.cache.requests", cache)
	antesUp := valorMetrica(t, "evaluation.upstream.requests", upstream)
	antesEvento := valorMetrica(t, "evaluation.events.published", evento)
	antesDecisao := valorMetrica(t, "evaluation.decisions", decisao)

	evalm.cacheResult("error")
	evalm.upstream("flag-service", "http_error", time.Now())
	evalm.eventPublished("disabled")
	evalm.decision(true)

	for nome, d := range map[string]int64{
		"evaluation.cache.requests":    valorMetrica(t, "evaluation.cache.requests", cache) - antesCache,
		"evaluation.upstream.requests": valorMetrica(t, "evaluation.upstream.requests", upstream) - antesUp,
		"evaluation.events.published":  valorMetrica(t, "evaluation.events.published", evento) - antesEvento,
		"evaluation.decisions":         valorMetrica(t, "evaluation.decisions", decisao) - antesDecisao,
	} {
		if d != 1 {
			t.Errorf("%s aumentou %d, esperado 1", nome, d)
		}
	}
}

// Fluxo real: cache miss -> flag-service fora -> métrica de upstream com erro.
func TestFetchFlag_RegistraUpstreamComErro(t *testing.T) {
	leitorDeMetricas(t)
	attrs := map[string]string{"upstream": "flag-service", "outcome": "error"}
	antes := valorMetrica(t, "evaluation.upstream.requests", attrs)

	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := srv.URL
	srv.Close() // conexão recusada

	app := &App{HttpClient: newHTTPClient(), FlagServiceURL: url}
	if _, err := app.fetchFlag(context.Background(), "x"); err == nil {
		t.Fatal("fetchFlag deveria falhar com o flag-service fora")
	}

	if got := valorMetrica(t, "evaluation.upstream.requests", attrs) - antes; got != 1 {
		t.Errorf("evaluation.upstream.requests{error} aumentou %d, esperado 1", got)
	}
}

// --- Setup -------------------------------------------------------------------

func preservarGlobais(t *testing.T) {
	t.Helper()
	logger := slog.Default()
	tp, mp, prop := otel.GetTracerProvider(), otel.GetMeterProvider(), otel.GetTextMapPropagator()
	t.Cleanup(func() {
		slog.SetDefault(logger)
		otel.SetTracerProvider(tp)
		otel.SetMeterProvider(mp)
		otel.SetTextMapPropagator(prop)
	})
}

func TestInitTelemetry_SemEndpointEhNoop(t *testing.T) {
	preservarGlobais(t)
	for _, v := range []string{"OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
		"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"} {
		t.Setenv(v, "")
	}

	shutdown := initTelemetry(context.Background())
	if err := shutdown(context.Background()); err != nil {
		t.Errorf("shutdown no-op devolveu erro: %v", err)
	}
}

func TestInitTelemetry_SdkDesligado(t *testing.T) {
	preservarGlobais(t)
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
	t.Setenv("OTEL_SDK_DISABLED", "true")

	if err := initTelemetry(context.Background())(context.Background()); err != nil {
		t.Errorf("shutdown com SDK desligado devolveu erro: %v", err)
	}
}

func TestInitTelemetry_ExportaOsTresSinaisNoShutdown(t *testing.T) {
	// Os instrumentos do pacote ficam no leitor em memória; o setup só troca o
	// provider global para os instrumentos criados depois dele.
	leitorDeMetricas(t)
	preservarGlobais(t)

	var mu sync.Mutex
	recebidos := map[string]int{}
	coletor := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		recebidos[r.URL.Path]++
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	defer coletor.Close()

	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", coletor.URL)
	t.Setenv("OTEL_METRIC_EXPORT_INTERVAL", "60000")

	shutdown := initTelemetry(context.Background())

	ctx, span := otel.Tracer("teste").Start(context.Background(), "requisicao")
	slog.InfoContext(ctx, "log de teste")
	span.End()

	if err := shutdown(context.Background()); err != nil {
		t.Fatalf("shutdown devolveu erro: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	for _, path := range []string{"/v1/traces", "/v1/metrics", "/v1/logs"} {
		if recebidos[path] == 0 {
			t.Errorf("o Collector falso não recebeu %s; recebido: %v", path, recebidos)
		}
	}
}

func TestInitTelemetry_SinalDesligadoNaoExporta(t *testing.T) {
	leitorDeMetricas(t)
	preservarGlobais(t)

	var mu sync.Mutex
	recebidos := map[string]int{}
	coletor := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		recebidos[r.URL.Path]++
		mu.Unlock()
	}))
	defer coletor.Close()

	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", coletor.URL)
	t.Setenv("OTEL_LOGS_EXPORTER", "none")
	t.Setenv("OTEL_METRICS_EXPORTER", "none")

	shutdown := initTelemetry(context.Background())
	slog.Info("não deveria sair por OTLP")
	if err := shutdown(context.Background()); err != nil {
		t.Fatalf("shutdown devolveu erro: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if recebidos["/v1/logs"] != 0 || recebidos["/v1/metrics"] != 0 {
		t.Errorf("sinais desligados foram exportados: %v", recebidos)
	}
}

// --- Logs ---------------------------------------------------------------------

func TestTraceHandler_AcrescentaTraceIDNoLog(t *testing.T) {
	comTracing(t)
	var buf bytes.Buffer
	logger := slog.New(traceHandler{slog.NewJSONHandler(&buf, nil)}).With("svc", "x").WithGroup("g")

	ctx, span := otel.Tracer("teste").Start(context.Background(), "requisicao")
	logger.InfoContext(ctx, "com trace", "k", "v")
	span.End()

	traceID := span.SpanContext().TraceID().String()
	if !strings.Contains(buf.String(), traceID) {
		t.Errorf("log sem o trace_id %s: %s", traceID, buf.String())
	}

	buf.Reset()
	logger.Info("sem trace")
	if strings.Contains(buf.String(), "trace_id") {
		t.Errorf("log fora de requisição não deveria ter trace_id: %s", buf.String())
	}
}

func TestFanoutHandler_EntregaParaTodosOsDestinos(t *testing.T) {
	var a, b bytes.Buffer
	soErros := &slog.HandlerOptions{Level: slog.LevelError}
	h := fanoutHandler{slog.NewJSONHandler(&a, nil), slog.NewJSONHandler(&b, soErros)}
	logger := slog.New(h).With("svc", "x").WithGroup("g")

	logger.Info("info")
	logger.Error("erro")

	if strings.Count(a.String(), "\n") != 2 {
		t.Errorf("destino A deveria ter 2 linhas: %q", a.String())
	}
	if strings.Count(b.String(), "\n") != 1 || !strings.Contains(b.String(), `"svc":"x"`) {
		t.Errorf("destino B deveria ter só o erro, com os atributos herdados: %q", b.String())
	}
	if h.Enabled(context.Background(), slog.LevelDebug) {
		t.Error("nenhum destino aceita DEBUG; Enabled deveria ser false")
	}
}
