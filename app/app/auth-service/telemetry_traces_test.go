package main

import (
	"bytes"
	"context"
	"database/sql/driver"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// traceparent de uma chamada vinda do flag-service (trace e span remotos fixos).
const (
	traceIDRemoto     = "4bf92f3577b34da6a3ce929d0e0e4736"
	spanIDRemoto      = "00f067aa0ba902b7"
	traceparentRemoto = "00-" + traceIDRemoto + "-" + spanIDRemoto + "-01"
)

// comTracing liga um TracerProvider em memória e o propagador W3C durante o
// teste, devolvendo o exportador para inspecionar os spans gerados.
func comTracing(t *testing.T) *tracetest.InMemoryExporter {
	t.Helper()
	exp := tracetest.NewInMemoryExporter()

	prevTP, prevProp := otel.GetTracerProvider(), otel.GetTextMapPropagator()
	otel.SetTracerProvider(sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp)))
	otel.SetTextMapPropagator(propagation.TraceContext{})
	t.Cleanup(func() {
		otel.SetTracerProvider(prevTP)
		otel.SetTextMapPropagator(prevProp)
	})
	return exp
}

func spanPorNome(t *testing.T, spans tracetest.SpanStubs, nome string) tracetest.SpanStub {
	t.Helper()
	for _, s := range spans {
		if s.Name == nome {
			return s
		}
	}
	nomes := make([]string, len(spans))
	for i, s := range spans {
		nomes[i] = s.Name
	}
	t.Fatalf("span %q não encontrado; spans gerados: %v", nome, nomes)
	return tracetest.SpanStub{}
}

func atributo(s tracetest.SpanStub, chave string) string {
	for _, kv := range s.Attributes {
		if kv.Key == attribute.Key(chave) {
			return kv.Value.Emit()
		}
	}
	return ""
}

func validarVindoDoFlagService(app *App) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, "/validate", nil)
	req.Header.Set("Authorization", "Bearer tm_key_qualquer")
	req.Header.Set("traceparent", traceparentRemoto)

	rec := httptest.NewRecorder()
	newTracedHandler(http.HandlerFunc(app.validateKeyHandler)).ServeHTTP(rec, req)
	return rec
}

// O flag-service e o targeting-service chamam o /validate com traceparent; o
// auth-service precisa continuar o MESMO trace, e a query no banco precisa ser
// filha da requisição — é isso que forma o caminho no trace distribuído.
func TestValidate_ContinuaOTraceRecebidoECriaSpanDoBanco(t *testing.T) {
	exp := comTracing(t)
	app, script := newTestApp(t)
	script.cols = []string{"id"}
	script.rows = [][]driver.Value{{int64(42)}}

	if rec := validarVindoDoFlagService(app); rec.Code != http.StatusOK {
		t.Fatalf("status = %d, esperado 200", rec.Code)
	}

	spans := exp.GetSpans()
	servidor := spanPorNome(t, spans, "GET /validate")
	banco := spanPorNome(t, spans, "SELECT api_keys")

	if servidor.SpanContext.TraceID().String() != traceIDRemoto {
		t.Errorf("trace do servidor = %s, esperado continuar %s", servidor.SpanContext.TraceID(), traceIDRemoto)
	}
	if servidor.Parent.SpanID().String() != spanIDRemoto {
		t.Errorf("pai do span de servidor = %s, esperado o span remoto %s", servidor.Parent.SpanID(), spanIDRemoto)
	}
	if banco.Parent.SpanID() != servidor.SpanContext.SpanID() {
		t.Error("o span do banco deveria ser filho do span da requisição")
	}
	if banco.SpanKind != trace.SpanKindClient {
		t.Errorf("kind do span do banco = %v, esperado Client", banco.SpanKind)
	}
	if got := atributo(banco, "db.system"); got != "postgresql" {
		t.Errorf("db.system = %q, esperado postgresql", got)
	}
	if got := atributo(banco, "db.statement"); got != sqlValidateKey {
		t.Errorf("db.statement = %q, esperado a query parametrizada", got)
	}
	if banco.Status.Code == codes.Error || servidor.Status.Code == codes.Error {
		t.Error("validação bem-sucedida não deveria marcar erro nos spans")
	}
}

// Banco fora do ar: a resposta continua 401 (comportamento original), mas o
// APM precisa ver o erro — senão a queda do banco some da taxa de erro.
func TestValidate_FalhaDoBancoMarcaErroMesmoRespondendo401(t *testing.T) {
	exp := comTracing(t)
	app, script := newTestApp(t)
	script.err = errors.New("conexão recusada")

	if rec := validarVindoDoFlagService(app); rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, esperado 401", rec.Code)
	}

	spans := exp.GetSpans()
	banco := spanPorNome(t, spans, "SELECT api_keys")
	servidor := spanPorNome(t, spans, "GET /validate")

	if banco.Status.Code != codes.Error {
		t.Errorf("status do span do banco = %v, esperado Error", banco.Status.Code)
	}
	if len(banco.Events) == 0 || banco.Events[0].Name != "exception" {
		t.Error("o span do banco deveria registrar a exceção")
	}
	if servidor.Status.Code != codes.Error {
		t.Errorf("status do span de servidor = %v, esperado Error", servidor.Status.Code)
	}
}

// Chave inexistente é resposta normal do serviço, não falha.
func TestValidate_ChaveInexistenteNaoEhErro(t *testing.T) {
	exp := comTracing(t)
	app, script := newTestApp(t)
	script.cols = []string{"id"} // sem linhas -> sql.ErrNoRows

	if rec := validarVindoDoFlagService(app); rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, esperado 401", rec.Code)
	}

	for _, s := range exp.GetSpans() {
		if s.Status.Code == codes.Error {
			t.Errorf("span %q marcado como erro para chave inexistente", s.Name)
		}
	}
}

func TestCreateKey_CriaSpanDoInsert(t *testing.T) {
	exp := comTracing(t)
	app, script := newTestApp(t)
	script.cols = []string{"id"}
	script.rows = [][]driver.Value{{int64(7)}}

	rec := httptest.NewRecorder()
	newTracedHandler(http.HandlerFunc(app.createKeyHandler)).ServeHTTP(rec, postCreateKey(t, `{"name":"flag-service"}`))
	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d, esperado 201 (corpo: %s)", rec.Code, rec.Body.String())
	}

	banco := spanPorNome(t, exp.GetSpans(), "INSERT api_keys")
	if got := atributo(banco, "db.operation"); got != "INSERT" {
		t.Errorf("db.operation = %q, esperado INSERT", got)
	}
}

func TestCreateKey_FalhaDoBancoMarcaErro(t *testing.T) {
	exp := comTracing(t)
	app, script := newTestApp(t)
	script.err = errors.New("disco cheio")

	rec := httptest.NewRecorder()
	newTracedHandler(http.HandlerFunc(app.createKeyHandler)).ServeHTTP(rec, postCreateKey(t, `{"name":"x"}`))
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, esperado 500", rec.Code)
	}

	if banco := spanPorNome(t, exp.GetSpans(), "INSERT api_keys"); banco.Status.Code != codes.Error {
		t.Errorf("status do span do insert = %v, esperado Error", banco.Status.Code)
	}
}

func TestTracedHandler_IgnoraHealth(t *testing.T) {
	exp := comTracing(t)
	app, _ := newTestApp(t)

	newTracedHandler(http.HandlerFunc(app.healthHandler)).
		ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/health", nil))

	if n := len(exp.GetSpans()); n != 0 {
		t.Errorf("o /health gerou %d span(s), esperado 0", n)
	}
}

// --- Logs ---------------------------------------------------------------------

func TestTraceHandler_AcrescentaTraceIDNoLog(t *testing.T) {
	comTracing(t)
	var buf bytes.Buffer
	logger := slog.New(traceHandler{slog.NewJSONHandler(&buf, nil)}).With("svc", "auth").WithGroup("g")

	ctx, span := otel.Tracer("teste").Start(context.Background(), "requisicao")
	logger.InfoContext(ctx, "com trace")
	span.End()

	if !strings.Contains(buf.String(), span.SpanContext().TraceID().String()) {
		t.Errorf("log sem o trace_id: %s", buf.String())
	}

	buf.Reset()
	logger.Info("sem trace")
	if strings.Contains(buf.String(), "trace_id") {
		t.Errorf("log fora de requisição não deveria ter trace_id: %s", buf.String())
	}
}

// --- Setup -------------------------------------------------------------------

func TestSetupTelemetry_ComEndpointExportaTracesNoShutdown(t *testing.T) {
	leitorDeMetricas(t)

	logger := slog.Default()
	tp, mp, prop := otel.GetTracerProvider(), otel.GetMeterProvider(), otel.GetTextMapPropagator()
	t.Cleanup(func() {
		slog.SetDefault(logger)
		otel.SetTracerProvider(tp)
		otel.SetMeterProvider(mp)
		otel.SetTextMapPropagator(prop)
	})

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
	t.Setenv("OTEL_METRICS_EXPORTER", "none")

	shutdown := setupTelemetry(context.Background())
	_, span := tracer().Start(context.Background(), "requisicao")
	span.End()
	if err := shutdown(context.Background()); err != nil {
		t.Fatalf("shutdown devolveu erro: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if recebidos["/v1/traces"] == 0 {
		t.Errorf("o Collector falso não recebeu traces; recebido: %v", recebidos)
	}
	if recebidos["/v1/metrics"] != 0 {
		t.Errorf("métricas desligadas (OTEL_METRICS_EXPORTER=none) foram exportadas: %v", recebidos)
	}
}

func TestSetupTelemetry_SdkDesligado(t *testing.T) {
	logger := slog.Default()
	t.Cleanup(func() { slog.SetDefault(logger) })
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
	t.Setenv("OTEL_SDK_DISABLED", "true")

	if err := setupTelemetry(context.Background())(context.Background()); err != nil {
		t.Errorf("shutdown com SDK desligado devolveu erro: %v", err)
	}
}
