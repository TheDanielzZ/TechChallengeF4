package main

import (
	"context"
	"net/http"
	"net/http/httptest"
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
// Os instrumentos do pacote (httpm, authm) nascem do MeterProvider global e são
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
		{"/validate", "/validate", true},
		{"/admin/keys", "/admin/keys", true},
		{"/health", "/health", false},
		{"/admin/keys/123", "unmatched", true},
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
	if got := methodLabel(http.MethodPost); got != http.MethodPost {
		t.Errorf("methodLabel(POST) = %q", got)
	}
	if got := methodLabel("BREW"); got != "OTHER" {
		t.Errorf("methodLabel(BREW) = %q, esperado OTHER", got)
	}
}

// --- Middleware HTTP ---------------------------------------------------------

func TestMetricsMiddleware_ContaRequisicaoPorRotaEStatus(t *testing.T) {
	leitorDeMetricas(t)
	attrs := map[string]string{"route": "/validate", "status_code": "401", "status_class": "4xx", "method": "GET"}
	antes := valorMetrica(t, "http.server.requests", attrs)
	antesDuracao := valorMetrica(t, "http.server.request.duration", attrs)

	h := metricsMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/validate", nil))

	if got := valorMetrica(t, "http.server.requests", attrs) - antes; got != 1 {
		t.Errorf("http.server.requests{401} aumentou %d, esperado 1", got)
	}
	if got := valorMetrica(t, "http.server.request.duration", attrs) - antesDuracao; got != 1 {
		t.Errorf("http.server.request.duration{401} ganhou %d observações, esperado 1", got)
	}
	if got := valorMetrica(t, "http.server.active_requests", nil); got != 0 {
		t.Errorf("http.server.active_requests = %d depois da requisição, esperado 0", got)
	}
}

func TestMetricsMiddleware_StatusImplicito200(t *testing.T) {
	leitorDeMetricas(t)
	attrs := map[string]string{"route": "/admin/keys", "status_code": "200"}
	antes := valorMetrica(t, "http.server.requests", attrs)

	h := metricsMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{}`)) // sem WriteHeader: o net/http assume 200
	}))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/admin/keys", nil))

	if got := valorMetrica(t, "http.server.requests", attrs) - antes; got != 1 {
		t.Errorf("http.server.requests{200} aumentou %d, esperado 1", got)
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
	attrs := map[string]string{"route": "/validate", "status_code": "500", "status_class": "5xx"}
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
		h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/validate", nil))
	}()

	if got := valorMetrica(t, "http.server.requests", attrs) - antes; got != 1 {
		t.Errorf("panic contado %d vez(es) como 5xx, esperado 1", got)
	}
}

func TestStatusRecorder_GuardaPrimeiroStatusEDesembrulha(t *testing.T) {
	inner := httptest.NewRecorder()
	rec := &statusRecorder{ResponseWriter: inner, status: http.StatusOK}

	rec.WriteHeader(http.StatusNotFound)
	rec.WriteHeader(http.StatusInternalServerError) // ignorado: o header já foi escrito

	if rec.status != http.StatusNotFound {
		t.Errorf("status = %d, esperado 404", rec.status)
	}
	if rec.Unwrap() != inner {
		t.Error("Unwrap deveria devolver o ResponseWriter original")
	}
}

// --- Métricas de negócio -----------------------------------------------------

func TestAuthMetrics_RegistraValidacaoEConsulta(t *testing.T) {
	leitorDeMetricas(t)
	antesValid := valorMetrica(t, "auth.key.validations", map[string]string{"result": "db_error"})
	antesQuery := valorMetrica(t, "auth.db.query.duration", map[string]string{"query": "validate_key"})

	authm.validation("db_error")
	authm.dbQuery("validate_key", time.Now())

	if got := valorMetrica(t, "auth.key.validations", map[string]string{"result": "db_error"}) - antesValid; got != 1 {
		t.Errorf("auth.key.validations{db_error} aumentou %d, esperado 1", got)
	}
	if got := valorMetrica(t, "auth.db.query.duration", map[string]string{"query": "validate_key"}) - antesQuery; got != 1 {
		t.Errorf("auth.db.query.duration{validate_key} ganhou %d observações, esperado 1", got)
	}
}

// --- Setup -------------------------------------------------------------------

func TestSetupTelemetry_SemEndpointEhNoop(t *testing.T) {
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
	t.Setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "")

	shutdown := setupTelemetry(context.Background())
	if err := shutdown(context.Background()); err != nil {
		t.Errorf("shutdown no-op devolveu erro: %v", err)
	}
}

func TestSetupTelemetry_ComEndpointExportaMetricasNoShutdown(t *testing.T) {
	// Garante que os instrumentos do pacote já estão ligados ao leitor em memória
	// antes de o setup trocar o provider global.
	leitorDeMetricas(t)

	var mu sync.Mutex
	recebidos := map[string]int{}
	coletor := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		recebidos[r.URL.Path]++
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	defer coletor.Close()

	anterior := otel.GetMeterProvider()
	t.Cleanup(func() { otel.SetMeterProvider(anterior) })

	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", coletor.URL)
	t.Setenv("OTEL_METRIC_EXPORT_INTERVAL", "60000")

	shutdown := setupTelemetry(context.Background())
	if err := shutdown(context.Background()); err != nil {
		t.Fatalf("shutdown devolveu erro: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if recebidos["/v1/metrics"] == 0 {
		t.Errorf("o Collector falso não recebeu métricas; recebido: %v", recebidos)
	}
}
