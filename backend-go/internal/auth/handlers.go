package auth

import (
	"errors"
	"log/slog"
	"net"
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/crypto/bcrypt"

	"github.com/go-chi/chi/v5/middleware"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/audit"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

const minPasswordLen = 10

// Handler serves /api/v1/auth/*.
type Handler struct {
	Pool                  *pgxpool.Pool
	Tokens                *TokenManager
	Audit                 *audit.Logger
	Log                   *slog.Logger
	AllowOpenRegistration bool
}

type credentialsReq struct {
	Email    string `json:"email"`
	Password string `json:"password"`
}

type userInfo struct {
	ID    string `json:"id"`
	Email string `json:"email"`
	Role  string `json:"role"`
}

// ValidateRegistration is the pure validation used by Register (unit tested).
func ValidateRegistration(email, password string) map[string]any {
	problems := map[string]any{}
	email = strings.TrimSpace(email)
	if email == "" || !strings.Contains(email, "@") || len(email) > 254 {
		problems["email"] = "must be a valid email address"
	}
	if len(password) < minPasswordLen {
		problems["password"] = "must be at least 10 characters"
	}
	if len(problems) == 0 {
		return nil
	}
	return problems
}

// Register creates a new user. The first user ever becomes admin; afterwards
// registration requires either ALLOW_OPEN_REGISTRATION=true or an admin JWT.
func (h *Handler) Register(w http.ResponseWriter, r *http.Request) {
	var req credentialsReq
	if !httpserver.DecodeJSON(w, r, &req) {
		return
	}
	req.Email = strings.ToLower(strings.TrimSpace(req.Email))
	if problems := ValidateRegistration(req.Email, req.Password); problems != nil {
		httpserver.BadRequest(w, "invalid registration payload", problems)
		return
	}

	ctx := r.Context()
	tx, err := h.Pool.Begin(ctx)
	if err != nil {
		h.Log.Error("register_begin_tx", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer func() { _ = tx.Rollback(ctx) }()

	// Serialize first-user detection so two concurrent registrations cannot
	// both become admin.
	if _, err := tx.Exec(ctx, `LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE`); err != nil {
		h.Log.Error("register_lock", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	var count int
	if err := tx.QueryRow(ctx, `SELECT count(*) FROM users`).Scan(&count); err != nil {
		h.Log.Error("register_count", "error", err)
		httpserver.Internal(w, "database error")
		return
	}

	role := "user"
	if count == 0 {
		role = "admin"
	} else if !h.AllowOpenRegistration {
		// Registration is closed: only an authenticated admin may add users.
		if !h.callerIsAdmin(r) {
			httpserver.Forbidden(w, "registration is closed; an admin must create accounts")
			return
		}
	}

	hash, err := bcrypt.GenerateFromPassword([]byte(req.Password), BcryptCost)
	if err != nil {
		h.Log.Error("register_bcrypt", "error", err)
		httpserver.Internal(w, "hashing error")
		return
	}

	var id string
	err = tx.QueryRow(ctx,
		`INSERT INTO users (email, password_hash, role) VALUES ($1, $2, $3) RETURNING id`,
		req.Email, string(hash), role).Scan(&id)
	if err != nil {
		if strings.Contains(err.Error(), "duplicate key") {
			httpserver.Conflict(w, "email already registered")
			return
		}
		h.Log.Error("register_insert", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	if err := tx.Commit(ctx); err != nil {
		h.Log.Error("register_commit", "error", err)
		httpserver.Internal(w, "database error")
		return
	}

	h.Audit.Record(ctx, audit.Entry{
		UserID: &id, Action: "user.register", Entity: "user", EntityID: id,
		Details:   map[string]any{"email": req.Email, "role": role},
		IP:        remoteIP(r),
		RequestID: middleware.GetReqID(ctx),
	})

	// Frontend expects the same envelope as login: token + user.
	token, exp, err := h.Tokens.Create(id, req.Email, role)
	if err != nil {
		h.Log.Error("register_token", "error", err)
		httpserver.Internal(w, "token error")
		return
	}
	httpserver.JSON(w, http.StatusCreated, map[string]any{
		"token":      token,
		"expires_at": exp.UTC(),
		"user":       userInfo{ID: id, Email: req.Email, Role: role},
	})
}

// callerIsAdmin verifies an optional bearer token for the register flow.
func (h *Handler) callerIsAdmin(r *http.Request) bool {
	tok := httpserver.BearerToken(r)
	if tok == "" {
		return false
	}
	claims, err := h.Tokens.Verify(tok)
	return err == nil && claims.Role == "admin"
}

// Login verifies credentials and returns a JWT.
func (h *Handler) Login(w http.ResponseWriter, r *http.Request) {
	var req credentialsReq
	if !httpserver.DecodeJSON(w, r, &req) {
		return
	}
	req.Email = strings.ToLower(strings.TrimSpace(req.Email))

	ctx := r.Context()
	var (
		id, hash, role string
	)
	err := h.Pool.QueryRow(ctx,
		`SELECT id, password_hash, role FROM users WHERE email = $1`, req.Email).
		Scan(&id, &hash, &role)
	if errors.Is(err, pgx.ErrNoRows) {
		// Constant-ish time: still burn a bcrypt compare on unknown users.
		_ = bcrypt.CompareHashAndPassword(
			[]byte("$2a$12$C6UzMDM.H6dfI/f/IKcEeO5C1shTf1e6EnFizJEyRkS3jJZDgIS9G"), []byte(req.Password))
		httpserver.Unauthorized(w, "invalid email or password")
		return
	}
	if err != nil {
		h.Log.Error("login_query", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	if bcrypt.CompareHashAndPassword([]byte(hash), []byte(req.Password)) != nil {
		httpserver.Unauthorized(w, "invalid email or password")
		return
	}

	token, exp, err := h.Tokens.Create(id, req.Email, role)
	if err != nil {
		h.Log.Error("login_token", "error", err)
		httpserver.Internal(w, "token error")
		return
	}

	h.Audit.Record(ctx, audit.Entry{
		UserID: &id, Action: "auth.login", Entity: "user", EntityID: id,
		IP: remoteIP(r), RequestID: middleware.GetReqID(ctx),
	})
	httpserver.JSON(w, http.StatusOK, map[string]any{
		"token":      token,
		"expires_at": exp.UTC(),
		"user":       userInfo{ID: id, Email: req.Email, Role: role},
	})
}

// Me returns the authenticated user's identity.
func (h *Handler) Me(w http.ResponseWriter, r *http.Request) {
	u, ok := httpserver.UserFromContext(r.Context())
	if !ok {
		httpserver.Unauthorized(w, "not authenticated")
		return
	}
	httpserver.JSON(w, http.StatusOK, userInfo{ID: u.ID, Email: u.Email, Role: u.Role})
}

// VerifyForMiddleware adapts TokenManager.Verify to httpserver.TokenVerifier.
func VerifyForMiddleware(tm *TokenManager) httpserver.TokenVerifier {
	return func(token string) (httpserver.AuthUser, error) {
		c, err := tm.Verify(token)
		if err != nil {
			return httpserver.AuthUser{}, err
		}
		return httpserver.AuthUser{ID: c.Sub, Email: c.Email, Role: c.Role}, nil
	}
}

func remoteIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}
