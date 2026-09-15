package catalog

import (
	"context"
	"errors"
	"time"
)

var ErrNotFound = errors.New("product not found")

type Product struct {
	ID          int64  `json:"id"`
	Name        string `json:"name"`
	Description string `json:"description"`
	PriceCents  int64  `json:"priceCents"`
}

type Repository interface {
	List(context.Context, int) ([]Product, error)
	Get(context.Context, int64) (Product, error)
}

type Cache interface {
	GetList(context.Context, int) ([]Product, bool, error)
	SetList(context.Context, int, []Product) error
	GetProduct(context.Context, int64) (Product, bool, error)
	SetProduct(context.Context, Product) error
}

type Metrics interface {
	ObserveDependency(dependency, operation, outcome string, duration time.Duration)
	RecordCache(operation, result string)
}
