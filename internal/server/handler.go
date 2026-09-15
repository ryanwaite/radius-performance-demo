package server

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"

	"github.com/ryanwaite/radius-performance-demo/internal/catalog"
)

type CatalogueService interface {
	List(context.Context, int) ([]catalog.Product, error)
	Get(context.Context, int64) (catalog.Product, error)
}

type Handler struct {
	service      CatalogueService
	maxListLimit int
}

func NewHandler(service CatalogueService, maxListLimit int) *Handler {
	return &Handler{service: service, maxListLimit: maxListLimit}
}

func (h *Handler) Register(mux *http.ServeMux, observe func(string, http.Handler) http.Handler) {
	mux.Handle("GET /api/products", observe("products.list", http.HandlerFunc(h.listProducts)))
	mux.Handle("GET /api/products/{id}", observe("products.get", http.HandlerFunc(h.getProduct)))
}

func (h *Handler) listProducts(w http.ResponseWriter, r *http.Request) {
	limit := 20
	if h.maxListLimit < limit {
		limit = h.maxListLimit
	}
	if value := r.URL.Query().Get("limit"); value != "" {
		parsed, err := strconv.Atoi(value)
		if err != nil || parsed < 1 || parsed > h.maxListLimit {
			writeError(w, http.StatusBadRequest, "limit must be between 1 and "+strconv.Itoa(h.maxListLimit))
			return
		}
		limit = parsed
	}

	products, err := h.service.List(r.Context(), limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "catalogue query failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"products": products,
		"count":    len(products),
	})
}

func (h *Handler) getProduct(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil || id < 1 {
		writeError(w, http.StatusBadRequest, "product id must be a positive integer")
		return
	}

	product, err := h.service.Get(r.Context(), id)
	if errors.Is(err, catalog.ErrNotFound) {
		writeError(w, http.StatusNotFound, "product not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "catalogue query failed")
		return
	}
	writeJSON(w, http.StatusOK, product)
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
