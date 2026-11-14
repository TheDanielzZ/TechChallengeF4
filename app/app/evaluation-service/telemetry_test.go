package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// comTracing liga um TracerProvider em memória e o propagador W3C durante o
// teste, devolvendo o exportador para inspecionar os spans gerados.
func comTracing(t *testing.T) *tracetest.InMemoryExporter {
	t.Helper()
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))

	prevTP, prevProp := otel.GetTracerProvider(), otel.GetTextMapPropagator()
	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.TraceContext{})
	t.Cleanup(func() {
		otel.SetTracerProvider(prevTP)
		otel.SetTextMapPropagator(prevProp)
	})
	return exp
}

// Sem o traceparent nas chamadas ao flag-service o trace distribuído quebra:
// cada serviço abriria um trace próprio e o Service Map ficaria sem arestas.
func TestFetchFlag_PropagaTraceparentParaOFlagService(t *testing.T) {
	comTracing(t)

	var traceparent string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		traceparent = r.Header.Get("traceparent")
		w.Write([]byte(`{"name":"checkout-novo","is_enabled":true}`))
	}))
	defer srv.Close()

	app := &App{HttpClient: newHTTPClient(), FlagServiceURL: srv.URL}

	ctx, span := otel.Tracer("teste").Start(context.Background(), "requisicao")
	_, err := app.fetchFlag(ctx, "checkout-novo")
	span.End()
	if err != nil {
		t.Fatalf("fetchFlag devolveu erro: %v", err)
	}

	traceID := span.SpanContext().TraceID().String()
	if !strings.Contains(traceparent, traceID) {
		t.Errorf("traceparent = %q, esperado conter o trace_id %s", traceparent, traceID)
	}
}

func TestTracedHandler_CriaSpanDeServidorEIgnoraHealth(t *testing.T) {
	exp := comTracing(t)

	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {})
	mux.HandleFunc("/evaluate", func(w http.ResponseWriter, r *http.Request) {})
	h := newTracedHandler(mux)

	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/health", nil))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/evaluate?user_id=u&flag_name=f", nil))

	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("spans gerados = %d, esperado 1 (só o /evaluate)", len(spans))
	}
	if spans[0].Name != "GET /evaluate" {
		t.Errorf("nome do span = %q, esperado \"GET /evaluate\"", spans[0].Name)
	}
}

// O analytics-service lê o contexto de trace das ApplicationProperties da
// mensagem para continuar o mesmo trace do outro lado da fila.
func TestMessageTraceProperties_LevaOContextoDoTrace(t *testing.T) {
	comTracing(t)

	ctx, span := otel.Tracer("teste").Start(context.Background(), "publicacao")
	defer span.End()

	props := messageTraceProperties(ctx)

	tp, ok := props["traceparent"].(string)
	if !ok || !strings.Contains(tp, span.SpanContext().TraceID().String()) {
		t.Fatalf("traceparent = %v, esperado conter o trace_id do span", props["traceparent"])
	}
	if props["Diagnostic-Id"] != tp {
		t.Errorf("Diagnostic-Id = %v, esperado igual ao traceparent %q", props["Diagnostic-Id"], tp)
	}
}

func TestMessageTraceProperties_SemTraceDevolveMapaVazio(t *testing.T) {
	comTracing(t)

	if props := messageTraceProperties(context.Background()); len(props) != 0 {
		t.Errorf("props = %v, esperado vazio sem span ativo", props)
	}
}
