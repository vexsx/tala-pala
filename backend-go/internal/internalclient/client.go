// Package internalclient is the HTTP client for the Python prediction
// service. Every request carries X-Internal-Token; 5xx responses and network
// errors are retried once.
package internalclient

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"
)

// Timeouts per CONTRACTS.md / task spec. Training walk-forward validates the
// full candidate roster over up to 40 folds per horizon for TWO symbols; on
// the production host a run takes 30–36 minutes, so the train timeout must
// comfortably exceed that (the caller's context must be at least as long —
// see scheduler job timeouts — because once() takes the tighter of the two).
const (
	TrainTimeout   = 90 * time.Minute
	CollectTimeout = 60 * time.Second
	DefaultTimeout = 60 * time.Second
)

// Client talks to the prediction-service internal API.
type Client struct {
	BaseURL string
	Token   string
	HTTP    *http.Client
	Log     *slog.Logger
}

// New creates a client. The http.Client has no global timeout; per-call
// timeouts are applied via context.
func New(baseURL, token string, log *slog.Logger) *Client {
	return &Client{BaseURL: baseURL, Token: token, HTTP: &http.Client{}, Log: log}
}

// APIError is a non-2xx response from the prediction service.
type APIError struct {
	Status int
	Body   string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("prediction-service returned %d: %s", e.Status, truncate(e.Body, 300))
}

func truncate(s string, n int) string {
	if len(s) > n {
		return s[:n] + "..."
	}
	return s
}

// Post sends a JSON POST and returns the raw response body. Retries once on
// 5xx or transport errors.
func (c *Client) Post(ctx context.Context, path string, body any, timeout time.Duration) (json.RawMessage, error) {
	var payload []byte
	if body != nil {
		var err error
		payload, err = json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("marshal request: %w", err)
		}
	}

	var lastErr error
	for attempt := 0; attempt < 2; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(2 * time.Second):
			}
			c.Log.Warn("internal_client_retry", "path", path, "error", lastErr.Error())
		}
		res, err := c.once(ctx, http.MethodPost, path, payload, timeout)
		if err == nil {
			return res, nil
		}
		lastErr = err
		var apiErr *APIError
		// Retry only transport errors and 5xx.
		if ok := asAPIError(err, &apiErr); ok && apiErr.Status < 500 {
			return nil, err
		}
	}
	return nil, lastErr
}

func asAPIError(err error, target **APIError) bool {
	if e, ok := err.(*APIError); ok {
		*target = e
		return true
	}
	return false
}

func (c *Client) once(ctx context.Context, method, path string, payload []byte, timeout time.Duration) (json.RawMessage, error) {
	reqCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	var bodyReader io.Reader
	if payload != nil {
		bodyReader = bytes.NewReader(payload)
	}
	req, err := http.NewRequestWithContext(reqCtx, method, c.BaseURL+path, bodyReader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-Internal-Token", c.Token)
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	respBody, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, &APIError{Status: resp.StatusCode, Body: string(respBody)}
	}
	if len(respBody) == 0 {
		respBody = []byte("{}")
	}
	return respBody, nil
}

// Convenience wrappers for each internal endpoint.

func (c *Client) Collect(ctx context.Context, jobs []string) (json.RawMessage, error) {
	if jobs == nil {
		jobs = []string{}
	}
	return c.Post(ctx, "/internal/collect", map[string]any{"jobs": jobs}, CollectTimeout)
}

func (c *Client) GenerateFeatures(ctx context.Context) (json.RawMessage, error) {
	return c.Post(ctx, "/internal/features/generate", nil, DefaultTimeout)
}

func (c *Client) Train(ctx context.Context, horizons []string) (json.RawMessage, error) {
	if horizons == nil {
		horizons = []string{}
	}
	return c.Post(ctx, "/internal/train", map[string]any{"horizons": horizons}, TrainTimeout)
}

func (c *Client) Predict(ctx context.Context, horizons []string) (json.RawMessage, error) {
	if horizons == nil {
		horizons = []string{}
	}
	return c.Post(ctx, "/internal/predict", map[string]any{"horizons": horizons}, DefaultTimeout)
}

// PredictCustom asks for an on-demand N-day forecast. It walk-forward
// validates fast model candidates on the fly, so it gets a training-class
// timeout rather than the default one.
func (c *Client) PredictCustom(ctx context.Context, days int) (json.RawMessage, error) {
	return c.Post(ctx, "/internal/predict/custom", map[string]any{"days": days}, 5*time.Minute)
}

func (c *Client) GenerateSignals(ctx context.Context) (json.RawMessage, error) {
	return c.Post(ctx, "/internal/signals/generate", nil, DefaultTimeout)
}

func (c *Client) Backtest(ctx context.Context, params json.RawMessage) (json.RawMessage, error) {
	var body any
	if len(params) > 0 {
		body = params
	} else {
		body = map[string]any{}
	}
	return c.Post(ctx, "/internal/backtest", body, TrainTimeout)
}

func (c *Client) Evaluate(ctx context.Context) (json.RawMessage, error) {
	return c.Post(ctx, "/internal/evaluate", nil, DefaultTimeout)
}

func (c *Client) Cleanup(ctx context.Context) (json.RawMessage, error) {
	return c.Post(ctx, "/internal/maintenance/cleanup", nil, DefaultTimeout)
}
