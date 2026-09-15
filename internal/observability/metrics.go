package observability

import (
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

type Metrics struct {
	httpRequests       *prometheus.CounterVec
	httpDuration       *prometheus.HistogramVec
	dependencyDuration *prometheus.HistogramVec
	cacheRequests      *prometheus.CounterVec
}

func New(registerer prometheus.Registerer) *Metrics {
	metrics := &Metrics{
		httpRequests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "catalog_http_requests_total",
			Help: "Total catalogue HTTP requests.",
		}, []string{"method", "route", "status"}),
		httpDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "catalog_http_request_duration_seconds",
			Help:    "Catalogue HTTP request latency in seconds.",
			Buckets: []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5},
		}, []string{"method", "route", "status"}),
		dependencyDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "catalog_dependency_request_duration_seconds",
			Help:    "Catalogue dependency request latency in seconds.",
			Buckets: []float64{0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5},
		}, []string{"dependency", "operation", "outcome"}),
		cacheRequests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "catalog_cache_requests_total",
			Help: "Cache-aside outcomes for catalogue operations.",
		}, []string{"operation", "result"}),
	}
	registerer.MustRegister(metrics.httpRequests, metrics.httpDuration, metrics.dependencyDuration, metrics.cacheRequests)
	return metrics
}

func (m *Metrics) ObserveDependency(dependency, operation, outcome string, duration time.Duration) {
	m.dependencyDuration.WithLabelValues(dependency, operation, outcome).Observe(duration.Seconds())
}

func (m *Metrics) RecordCache(operation, result string) {
	m.cacheRequests.WithLabelValues(operation, result).Inc()
}

func (m *Metrics) ObserveHTTP(route string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		recorder := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(recorder, r)
		status := strconv.Itoa(recorder.status)
		m.httpRequests.WithLabelValues(r.Method, route, status).Inc()
		m.httpDuration.WithLabelValues(r.Method, route, status).Observe(time.Since(start).Seconds())
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(status int) {
	r.status = status
	r.ResponseWriter.WriteHeader(status)
}
