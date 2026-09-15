package catalog

import "context"

type Service struct {
	repository   Repository
	cache        Cache
	cacheEnabled bool
	metrics      Metrics
}

func NewService(repository Repository, cache Cache, cacheEnabled bool, metrics Metrics) *Service {
	return &Service{
		repository:   repository,
		cache:        cache,
		cacheEnabled: cacheEnabled && cache != nil,
		metrics:      metrics,
	}
}

func (s *Service) List(ctx context.Context, limit int) ([]Product, error) {
	if s.cacheEnabled {
		products, found, err := s.cache.GetList(ctx, limit)
		if err != nil {
			s.recordCache("list", "error")
		} else if found {
			s.recordCache("list", "hit")
			return products, nil
		} else {
			s.recordCache("list", "miss")
		}
	}

	products, err := s.repository.List(ctx, limit)
	if err != nil {
		return nil, err
	}
	if s.cacheEnabled {
		if err := s.cache.SetList(ctx, limit, products); err != nil {
			s.recordCache("list_set", "error")
		}
	}
	return products, nil
}

func (s *Service) Get(ctx context.Context, id int64) (Product, error) {
	if s.cacheEnabled {
		product, found, err := s.cache.GetProduct(ctx, id)
		if err != nil {
			s.recordCache("get", "error")
		} else if found {
			s.recordCache("get", "hit")
			return product, nil
		} else {
			s.recordCache("get", "miss")
		}
	}

	product, err := s.repository.Get(ctx, id)
	if err != nil {
		return Product{}, err
	}
	if s.cacheEnabled {
		if err := s.cache.SetProduct(ctx, product); err != nil {
			s.recordCache("get_set", "error")
		}
	}
	return product, nil
}

func (s *Service) recordCache(operation, result string) {
	if s.metrics != nil {
		s.metrics.RecordCache(operation, result)
	}
}
