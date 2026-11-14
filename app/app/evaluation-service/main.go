package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/Azure/azure-sdk-for-go/sdk/messaging/azservicebus"
	"github.com/go-redis/redis/extra/redisotel/v8"
	"github.com/go-redis/redis/v8"
	"github.com/joho/godotenv"
)

var ctx = context.Background()

// App struct — ServiceBusClient substitui SqsSvc do original.
// Redis, HttpClient e URLs dos serviços permanecem iguais.
type App struct {
	RedisClient         *redis.Client
	ServiceBusClient    *azservicebus.Client
	ServiceBusQueueName string
	HttpClient          *http.Client
	FlagServiceURL      string
	TargetingServiceURL string
}

func main() {
	_ = godotenv.Load()

	// Telemetria (traces -> APM, métricas -> Prometheus). No-op se
	// OTEL_EXPORTER_OTLP_ENDPOINT não estiver definido.
	shutdownTelemetry := initTelemetry(ctx)

	port := os.Getenv("PORT")
	if port == "" {
		port = "8004"
	}

	redisURL := os.Getenv("REDIS_URL")
	if redisURL == "" {
		log.Fatal("REDIS_URL deve ser definida")
	}

	flagSvcURL := os.Getenv("FLAG_SERVICE_URL")
	if flagSvcURL == "" {
		log.Fatal("FLAG_SERVICE_URL deve ser definida")
	}

	targetingSvcURL := os.Getenv("TARGETING_SERVICE_URL")
	if targetingSvcURL == "" {
		log.Fatal("TARGETING_SERVICE_URL deve ser definida")
	}

	// Service Bus é opcional — se não configurado, eventos são apenas logados.
	// O serviço principal (avaliar flags) funciona normalmente sem ele.
	serviceBusConnStr := os.Getenv("SERVICE_BUS_CONNECTION_STRING")
	serviceBusQueueName := os.Getenv("SERVICE_BUS_QUEUE_NAME")
	if serviceBusConnStr == "" {
		log.Println("Atenção: SERVICE_BUS_CONNECTION_STRING não definida. Eventos serão apenas logados.")
	}

	// --- Redis ---
	opt, err := redis.ParseURL(redisURL)
	if err != nil {
		log.Fatalf("Erro ao parsear REDIS_URL: %v", err)
	}
	rdb := redis.NewClient(opt)
	rdb.AddHook(redisotel.NewTracingHook())
	if _, err := rdb.Ping(ctx).Result(); err != nil {
		log.Fatalf("Não foi possível conectar ao Redis: %v", err)
	}
	log.Println("Conectado ao Redis com sucesso!")

	// --- Azure Service Bus ---
	var sbClient *azservicebus.Client
	if serviceBusConnStr != "" {
		sbClient, err = azservicebus.NewClientFromConnectionString(serviceBusConnStr, nil)
		if err != nil {
			log.Fatalf("Erro ao criar cliente do Service Bus: %v", err)
		}
		log.Printf("Cliente Azure Service Bus inicializado. Fila: %s", serviceBusQueueName)
	}

	app := &App{
		RedisClient:         rdb,
		ServiceBusClient:    sbClient,
		ServiceBusQueueName: serviceBusQueueName,
		HttpClient:          newHTTPClient(),
		FlagServiceURL:      flagSvcURL,
		TargetingServiceURL: targetingSvcURL,
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/health", app.healthHandler)
	mux.HandleFunc("/evaluate", app.evaluationHandler)

	// O span de servidor fica por fora: a requisição já está no trace quando
	// as métricas HTTP são registradas.
	server := &http.Server{Addr: ":" + port, Handler: newTracedHandler(metricsMiddleware(mux))}
	go func() {
		log.Printf("Serviço de Avaliação (Go) rodando na porta %s", port)
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatal(err)
		}
	}()

	// Espera o SIGTERM do Kubernetes para drenar as requisições e enviar a
	// telemetria que ainda está no buffer antes de o Pod morrer.
	stop, cancel := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer cancel()
	<-stop.Done()

	shutdownCtx, cancelShutdown := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancelShutdown()
	if err := server.Shutdown(shutdownCtx); err != nil {
		log.Printf("Erro ao encerrar o servidor HTTP: %v", err)
	}
	if err := shutdownTelemetry(shutdownCtx); err != nil {
		log.Printf("Erro ao enviar a telemetria pendente: %v", err)
	}
}
