package main

import (
	"context"
	"database/sql"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	_ "github.com/jackc/pgx/v4/stdlib"
	"github.com/joho/godotenv"
)

// App struct (para injeção de dependência)
type App struct {
	DB         *sql.DB
	MasterKey  string
}

func main() {
	// Carrega o .env para desenvolvimento local. Em produção, isso não fará nada.
	_ = godotenv.Load()

	// --- Configuração ---
	port := os.Getenv("PORT")
	if port == "" {
		port = "8001" // Porta padrão
	}

	databaseURL := os.Getenv("DATABASE_URL")
	if databaseURL == "" {
		log.Fatal("DATABASE_URL deve ser definida")
	}

	masterKey := os.Getenv("MASTER_KEY")
	if masterKey == "" {
		log.Fatal("MASTER_KEY deve ser definida")
	}

	// --- Telemetria (traces -> APM, métricas -> Prometheus) ---
	// No-op se OTEL_EXPORTER_OTLP_ENDPOINT não estiver definido.
	shutdownTelemetry := setupTelemetry(context.Background())

	// --- Conexão com o Banco ---
	db, err := connectDB(databaseURL)
	if err != nil {
		log.Fatalf("Não foi possível conectar ao banco de dados: %v", err)
	}
	defer db.Close()

	app := &App{
		DB:         db,
		MasterKey:  masterKey,
	}

	// --- Rotas da API ---
	mux := http.NewServeMux()
	mux.HandleFunc("/health", app.healthHandler)

	// Endpoint público para validar uma chave
	mux.HandleFunc("/validate", app.validateKeyHandler)

	// Endpoints de "admin" para criar/gerenciar chaves
	// Eles são protegidos pelo middleware de autenticação
	mux.Handle("/admin/keys", app.masterKeyAuthMiddleware(http.HandlerFunc(app.createKeyHandler)))

	// Servidor com shutdown gracioso: no SIGTERM (rollout/restart do pod) drenamos as
	// requisições e damos flush na telemetria pendente antes de sair. O span de
	// servidor fica por fora: a requisição já está no trace quando as métricas
	// HTTP são registradas.
	srv := &http.Server{Addr: ":" + port, Handler: newTracedHandler(metricsMiddleware(mux))}

	sigCtx, stopSignals := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stopSignals()
	shutdownDone := make(chan struct{})
	go func() {
		<-sigCtx.Done()
		shCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shCtx)
		close(shutdownDone)
	}()

	log.Printf("Serviço de Autenticação (Go) rodando na porta %s", port)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
	<-shutdownDone // aguarda a drenagem das requisições em andamento

	flushCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := shutdownTelemetry(flushCtx); err != nil {
		log.Printf("Erro ao finalizar telemetria: %v", err)
	}
}

// connectDB inicializa e testa a conexão com o PostgreSQL
func connectDB(databaseURL string) (*sql.DB, error) {
	db, err := sql.Open("pgx", databaseURL)
	if err != nil {
		return nil, err
	}

	if err = db.Ping(); err != nil {
		return nil, err
	}

	log.Println("Conectado ao PostgreSQL com sucesso!")
	return db, nil
}