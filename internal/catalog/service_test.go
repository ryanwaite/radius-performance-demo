package catalog

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestServiceListCacheHitSkipsRepository(t *testing.T) {
	want := []Product{{ID: 1, Name: "Cached"}}
	repository := &fakeRepository{listErr: errors.New("repository should not be called")}
	cache := &fakeCache{list: want, listFound: true}
	metrics := &fakeMetrics{}
	service := NewService(repository, cache, true, metrics)

	got, err := service.List(context.Background(), 20)
	if err != nil {
		t.Fatalf("List() error = %v", err)
	}
	if len(got) != 1 || got[0].Name != want[0].Name {
		t.Fatalf("List() = %#v, want %#v", got, want)
	}
	if repository.listCalls != 0 {
		t.Fatalf("repository calls = %d, want 0", repository.listCalls)
	}
	if metrics.cacheResults["list:hit"] != 1 {
		t.Fatalf("cache hit metric = %d, want 1", metrics.cacheResults["list:hit"])
	}
}

func TestServiceGetCacheMissLoadsAndPopulates(t *testing.T) {
	want := Product{ID: 7, Name: "Database"}
	repository := &fakeRepository{product: want}
	cache := &fakeCache{}
	metrics := &fakeMetrics{}
	service := NewService(repository, cache, true, metrics)

	got, err := service.Get(context.Background(), want.ID)
	if err != nil {
		t.Fatalf("Get() error = %v", err)
	}
	if got != want {
		t.Fatalf("Get() = %#v, want %#v", got, want)
	}
	if repository.getCalls != 1 || cache.setProductCalls != 1 {
		t.Fatalf("repository calls = %d, cache sets = %d; want 1 each", repository.getCalls, cache.setProductCalls)
	}
	if metrics.cacheResults["get:miss"] != 1 {
		t.Fatalf("cache miss metric = %d, want 1", metrics.cacheResults["get:miss"])
	}
}

func TestServiceCacheErrorFallsBackToRepository(t *testing.T) {
	want := Product{ID: 8, Name: "Fallback"}
	repository := &fakeRepository{product: want}
	cache := &fakeCache{productErr: errors.New("valkey unavailable")}
	metrics := &fakeMetrics{}
	service := NewService(repository, cache, true, metrics)

	got, err := service.Get(context.Background(), want.ID)
	if err != nil {
		t.Fatalf("Get() error = %v", err)
	}
	if got != want {
		t.Fatalf("Get() = %#v, want %#v", got, want)
	}
	if metrics.cacheResults["get:error"] != 1 {
		t.Fatalf("cache error metric = %d, want 1", metrics.cacheResults["get:error"])
	}
}

type fakeRepository struct {
	list      []Product
	listErr   error
	product   Product
	getErr    error
	listCalls int
	getCalls  int
}

func (f *fakeRepository) List(context.Context, int) ([]Product, error) {
	f.listCalls++
	return f.list, f.listErr
}

func (f *fakeRepository) Get(context.Context, int64) (Product, error) {
	f.getCalls++
	return f.product, f.getErr
}

type fakeCache struct {
	list            []Product
	listFound       bool
	listErr         error
	product         Product
	productFound    bool
	productErr      error
	setListErr      error
	setProductErr   error
	setListCalls    int
	setProductCalls int
}

func (f *fakeCache) GetList(context.Context, int) ([]Product, bool, error) {
	return f.list, f.listFound, f.listErr
}

func (f *fakeCache) SetList(context.Context, int, []Product) error {
	f.setListCalls++
	return f.setListErr
}

func (f *fakeCache) GetProduct(context.Context, int64) (Product, bool, error) {
	return f.product, f.productFound, f.productErr
}

func (f *fakeCache) SetProduct(context.Context, Product) error {
	f.setProductCalls++
	return f.setProductErr
}

type fakeMetrics struct {
	cacheResults map[string]int
}

func (f *fakeMetrics) ObserveDependency(string, string, string, time.Duration) {}

func (f *fakeMetrics) RecordCache(operation, result string) {
	if f.cacheResults == nil {
		f.cacheResults = make(map[string]int)
	}
	f.cacheResults[operation+":"+result]++
}
