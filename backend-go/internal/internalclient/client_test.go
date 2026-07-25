package internalclient

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func testLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// swapRetryDelay shortens the inter-attempt pause for the duration of a test.
func swapRetryDelay(t *testing.T, d time.Duration) {
	t.Helper()
	prev := retryDelay
	retryDelay = d
	t.Cleanup(func() { retryDelay = prev })
}

// wrapped mimics what http.Client does to a transport error.
func wrapped(err error) error {
	return &url.Error{Op: "Post", URL: "http://prediction-service:8500/internal/train", Err: err}
}

func TestUndeliveredClassification(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want bool
	}{
		// Nothing was written to a connection: replaying cannot duplicate work.
		{"dial refused", &net.OpError{Op: "dial", Err: errors.New("connection refused")}, true},
		{"dial refused wrapped by http.Client", wrapped(&net.OpError{Op: "dial", Err: errors.New("connection refused")}), true},
		{"dns lookup failed", wrapped(&net.DNSError{Err: "no such host", Name: "prediction-service"}), true},
		{"dial timed out", wrapped(&net.OpError{Op: "dial", Err: context.DeadlineExceeded}), true},

		// The job may already be running on the Python side.
		{"per-call timeout", wrapped(context.DeadlineExceeded), false},
		{"caller cancelled", wrapped(context.Canceled), false},
		{"connection reset while reading the response", wrapped(&net.OpError{Op: "read", Err: errors.New("connection reset by peer")}), false},
		{"write failed mid-request", wrapped(&net.OpError{Op: "write", Err: errors.New("broken pipe")}), false},
		{"truncated response body", io.ErrUnexpectedEOF, false},

		// A status code means the service dispatched the request.
		{"500 internal error", &APIError{Status: 500, Body: "traceback"}, false},
		{"502 bad gateway", &APIError{Status: 502}, false},
		{"503 unavailable", &APIError{Status: 503}, false},
		{"504 gateway timeout", &APIError{Status: 504}, false},
		{"400 bad request", &APIError{Status: 400}, false},
		{"401 bad token", &APIError{Status: 401}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := undelivered(tc.err); got != tc.want {
				t.Fatalf("undelivered(%v) = %v, want %v", tc.err, got, tc.want)
			}
		})
	}
}

// countingTransport fails the first n round trips with err, then serves 200.
type countingTransport struct {
	calls    atomic.Int32
	failures int32
	err      error
}

func (tr *countingTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	if tr.calls.Add(1) <= tr.failures {
		return nil, tr.err
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Body:       io.NopCloser(strings.NewReader(`{"ok":true}`)),
		Header:     http.Header{},
		Request:    req,
	}, nil
}

func TestPostRetriesUndeliveredRequestOnce(t *testing.T) {
	swapRetryDelay(t, time.Millisecond)
	tr := &countingTransport{failures: 1, err: &net.OpError{Op: "dial", Err: errors.New("connection refused")}}
	c := New("http://prediction-service:8500", "tok", testLogger())
	c.HTTP = &http.Client{Transport: tr}

	body, err := c.Train(context.Background(), nil)
	if err != nil {
		t.Fatalf("second attempt should have succeeded: %v", err)
	}
	if string(body) != `{"ok":true}` {
		t.Fatalf("body = %s", body)
	}
	if got := tr.calls.Load(); got != 2 {
		t.Fatalf("attempts = %d, want 2 (one retry after a dial failure)", got)
	}
}

func TestPostGivesUpAfterTwoUndeliveredAttempts(t *testing.T) {
	swapRetryDelay(t, time.Millisecond)
	tr := &countingTransport{failures: 99, err: &net.OpError{Op: "dial", Err: errors.New("connection refused")}}
	c := New("http://prediction-service:8500", "tok", testLogger())
	c.HTTP = &http.Client{Transport: tr}

	if _, err := c.Collect(context.Background(), nil); err == nil {
		t.Fatal("want an error when both attempts fail to connect")
	}
	if got := tr.calls.Load(); got != 2 {
		t.Fatalf("attempts = %d, want exactly 2", got)
	}
}

func TestPostDoesNotRetryServerError(t *testing.T) {
	swapRetryDelay(t, time.Millisecond)
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		http.Error(w, "training already in progress", http.StatusInternalServerError)
	}))
	defer srv.Close()

	c := New(srv.URL, "tok", testLogger())
	_, err := c.Train(context.Background(), nil)
	var apiErr *APIError
	if !errors.As(err, &apiErr) || apiErr.Status != http.StatusInternalServerError {
		t.Fatalf("err = %v, want APIError 500", err)
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("service saw %d requests, want 1: a 5xx means the handler already ran", got)
	}
}

func TestPostDoesNotRetryTimeout(t *testing.T) {
	swapRetryDelay(t, time.Millisecond)
	var calls atomic.Int32
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		// Stand in for a synchronous Python handler that keeps working after
		// the client has walked away.
		select {
		case <-release:
		case <-r.Context().Done():
		}
	}))
	defer srv.Close()
	defer close(release)

	c := New(srv.URL, "tok", testLogger())
	_, err := c.Post(context.Background(), "/internal/collect", map[string]any{"jobs": []string{}}, 50*time.Millisecond)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("err = %v, want context.DeadlineExceeded", err)
	}
	// Give a would-be retry (retryDelay is 1ms here) time to land.
	time.Sleep(50 * time.Millisecond)
	if got := calls.Load(); got != 1 {
		t.Fatalf("service saw %d requests, want 1: a timeout must not start a second run", got)
	}
}
