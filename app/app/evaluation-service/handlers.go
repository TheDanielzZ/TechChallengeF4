package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

type EvaluationResponse struct {
	FlagName string `json:"flag_name"`
	UserID   string `json:"user_id"`
	Result   bool   `json:"result"`
}

func (a *App) healthHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

func (a *App) evaluationHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	ctx := r.Context()
	span := trace.SpanFromContext(ctx)

	// 1. Parsear os query parameters
	userID := r.URL.Query().Get("user_id")
	flagName := r.URL.Query().Get("flag_name")

	if userID == "" || flagName == "" {
		http.Error(w, `{"error": "user_id e flag_name são obrigatórios"}`, http.StatusBadRequest)
		return
	}

	// Atributos de negócio no span: permitem filtrar traces por flag no APM.
	span.SetAttributes(attribute.String("toggle.flag_name", flagName))

	// 2. Obter a decisão (lógica de cache/serviço está em evaluator.go)
	result, err := a.getDecision(ctx, userID, flagName)
	if err != nil {
		// Se o erro for "não encontrado", retornamos 'false' (comportamento seguro)
		if _, ok := err.(*NotFoundError); ok {
			result = false
		} else {
			// Outros erros (serviços offline, etc)
			slog.ErrorContext(ctx, "Erro ao avaliar flag", "flag_name", flagName, "error", err)
			span.RecordError(err)
			span.SetStatus(codes.Error, "falha ao avaliar a flag")
			http.Error(w, `{"error": "Erro interno ao avaliar a flag"}`, http.StatusBadGateway)
			return
		}
	}

	span.SetAttributes(attribute.Bool("toggle.result", result))
	evalm.decision(result)

	// 3. Enviar evento para o Service Bus (assincronamente)
	// Isso não bloqueia a resposta para o cliente. WithoutCancel mantém o trace
	// da requisição sem herdar o cancelamento que acontece quando ela termina.
	go a.sendEvaluationEvent(context.WithoutCancel(ctx), userID, flagName, result)

	// 4. Retornar a resposta
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(EvaluationResponse{
		FlagName: flagName,
		UserID:   userID,
		Result:   result,
	})
}