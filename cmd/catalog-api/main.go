package main

import (
	"context"
	"database/sql"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	_ "github.com/go-sql-driver/mysql"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/redis/go-redis/v9"

	"github.com/ryanwaite/radius-performance-demo/internal/catalog"
	"github.com/ryanwaite/radius-performance-demo/internal/config"
	"github.com/ryanwaite/radius-performance-demo/internal/observability"
	"github.com/ryanwaite/radius-performance-demo/internal/server"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("load configuration: %v", err)
	}

	db, err := sql.Open("mysql", cfg.MySQLDSN)
	if err != nil {
		log.Fatalf("open MySQL: %v", err)
	}
	defer db.Close()
	db.SetMaxOpenConns(cfg.DBMaxOpenConns)
	db.SetMaxIdleConns(cfg.DBMaxIdleConns)
	db.SetConnMaxLifetime(cfg.DBConnMaxLifetime)

	mysqlStartupCtx, mysqlStartupCancel := context.WithTimeout(context.Background(), cfg.DependencyTimeout)
	if err := db.PingContext(mysqlStartupCtx); err != nil {
		mysqlStartupCancel()
		log.Fatalf("ping MySQL: %v", err)
	}
	mysqlStartupCancel()

	metrics := observability.New(prometheus.DefaultRegisterer)
	repository := catalog.NewMySQLRepository(db, cfg.DBReadDelay, cfg.DependencyTimeout, metrics)

	var productCache catalog.Cache
	var redisClient *redis.Client
	if cfg.CacheEnabled {
		redisClient = redis.NewClient(&redis.Options{
			Addr:         cfg.ValkeyAddress,
			DialTimeout:  cfg.DependencyTimeout,
			ReadTimeout:  cfg.DependencyTimeout,
			WriteTimeout: cfg.DependencyTimeout,
		})
		valkeyStartupCtx, valkeyStartupCancel := context.WithTimeout(context.Background(), cfg.DependencyTimeout)
		if err := redisClient.Ping(valkeyStartupCtx).Err(); err != nil {
			log.Printf("Valkey unavailable at startup; cache reads will fail open to MySQL: %v", err)
		}
		valkeyStartupCancel()
		defer redisClient.Close()
		productCache = catalog.NewRedisCache(redisClient, cfg.CacheTTL, cfg.DependencyTimeout, metrics)
	}

	service := catalog.NewService(repository, productCache, cfg.CacheEnabled, metrics)
	apiHandler := server.NewHandler(service, cfg.MaxListLimit)
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) {
		readyCtx, readyCancel := context.WithTimeout(context.Background(), cfg.DependencyTimeout)
		defer readyCancel()
		w.Header().Set("Content-Type", "application/json")
		if err := db.PingContext(readyCtx); err != nil {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"status":"unavailable"}`))
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"ready"}`))
	})
	apiHandler.Register(mux, metrics.ObserveHTTP)
	mux.Handle("GET /metrics", promhttp.Handler())

	httpServer := &http.Server{
		Addr:              cfg.ListenAddress,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	runCtx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-runCtx.Done()
		shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
		defer shutdownCancel()
		if err := httpServer.Shutdown(shutdownCtx); err != nil {
			log.Printf("HTTP shutdown: %v", err)
		}
	}()

	log.Printf("catalog API listening on %s (cache enabled: %t)", cfg.ListenAddress, cfg.CacheEnabled)
	if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("serve HTTP: %v", err)
	}
}
