package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/ryanwaite/radius-performance-demo/internal/catalog"
)

func TestListProducts(t *testing.T) {
	service := &fakeService{products: []catalog.Product{{ID: 1, Name: "Keyboard"}}}
	mux := http.NewServeMux()
	NewHandler(service, 100).Register(mux, passthrough)
	request := httptest.NewRequest(http.MethodGet, "/api/products?limit=5", nil)
	response := httptest.NewRecorder()

	mux.ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusOK)
	}
	if service.limit != 5 {
		t.Fatalf("limit = %d, want 5", service.limit)
	}
	var body struct {
		Count int `json:"count"`
	}
	if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if body.Count != 1 {
		t.Fatalf("count = %d, want 1", body.Count)
	}
}

func TestListProductsRejectsUnboundedLimit(t *testing.T) {
	mux := http.NewServeMux()
	NewHandler(&fakeService{}, 100).Register(mux, passthrough)
	request := httptest.NewRequest(http.MethodGet, "/api/products?limit=101", nil)
	response := httptest.NewRecorder()

	mux.ServeHTTP(response, request)

	if response.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusBadRequest)
	}
}

func TestListProductsDefaultRespectsConfiguredMaximum(t *testing.T) {
	service := &fakeService{}
	mux := http.NewServeMux()
	NewHandler(service, 10).Register(mux, passthrough)
	request := httptest.NewRequest(http.MethodGet, "/api/products", nil)
	response := httptest.NewRecorder()

	mux.ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusOK)
	}
	if service.limit != 10 {
		t.Fatalf("limit = %d, want 10", service.limit)
	}
}

func TestGetProductNotFound(t *testing.T) {
	mux := http.NewServeMux()
	NewHandler(&fakeService{getErr: catalog.ErrNotFound}, 100).Register(mux, passthrough)
	request := httptest.NewRequest(http.MethodGet, "/api/products/42", nil)
	response := httptest.NewRecorder()

	mux.ServeHTTP(response, request)

	if response.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusNotFound)
	}
}

func passthrough(_ string, handler http.Handler) http.Handler {
	return handler
}

type fakeService struct {
	products []catalog.Product
	product  catalog.Product
	getErr   error
	limit    int
}

func (f *fakeService) List(_ context.Context, limit int) ([]catalog.Product, error) {
	f.limit = limit
	return f.products, nil
}

func (f *fakeService) Get(context.Context, int64) (catalog.Product, error) {
	return f.product, f.getErr
}
