package admin

import (
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/audit"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/auth"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// Admin user management: the dashboard's Users tab. Registration is closed
// (ALLOW_OPEN_REGISTRATION=false); every account is created here. Safety
// rails: an admin can never delete their own account, and the last admin can
// neither be deleted nor demoted.

type adminUser struct {
	ID        string    `json:"id"`
	Email     string    `json:"email"`
	Role      string    `json:"role"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
	// Transactions lets the UI warn before deleting an account with data.
	Transactions int `json:"transactions"`
}

var validRoles = map[string]bool{"user": true, "admin": true}

// ListUsers implements GET /api/v1/admin/users.
func (h *Handler) ListUsers(w http.ResponseWriter, r *http.Request) {
	rows, err := h.Pool.Query(r.Context(), `
		SELECT u.id, u.email, u.role, u.created_at, u.updated_at,
		       count(t.id) AS transactions
		FROM users u
		LEFT JOIN portfolio_transactions t ON t.user_id = u.id
		GROUP BY u.id
		ORDER BY u.created_at ASC`)
	if err != nil {
		h.Log.Error("admin_users_list", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	items := []adminUser{}
	for rows.Next() {
		var u adminUser
		if err := rows.Scan(&u.ID, &u.Email, &u.Role, &u.CreatedAt, &u.UpdatedAt, &u.Transactions); err != nil {
			h.Log.Error("admin_users_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		u.CreatedAt = u.CreatedAt.UTC()
		u.UpdatedAt = u.UpdatedAt.UTC()
		items = append(items, u)
	}
	if err := rows.Err(); err != nil {
		h.Log.Error("admin_users_rows", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, map[string]any{"items": items})
}

type createUserReq struct {
	Email    string `json:"email"`
	Password string `json:"password"`
	Role     string `json:"role"`
}

// CreateUser implements POST /api/v1/admin/users.
func (h *Handler) CreateUser(w http.ResponseWriter, r *http.Request) {
	var req createUserReq
	if !httpserver.DecodeJSON(w, r, &req) {
		return
	}
	req.Email = strings.ToLower(strings.TrimSpace(req.Email))
	if req.Role == "" {
		req.Role = "user"
	}
	if problems := auth.ValidateRegistration(req.Email, req.Password); problems != nil {
		httpserver.BadRequest(w, "invalid user payload", problems)
		return
	}
	if !validRoles[req.Role] {
		httpserver.BadRequest(w, "role must be 'user' or 'admin'", map[string]any{"role": req.Role})
		return
	}

	hash, err := bcrypt.GenerateFromPassword([]byte(req.Password), auth.BcryptCost)
	if err != nil {
		h.Log.Error("admin_user_bcrypt", "error", err)
		httpserver.Internal(w, "hashing error")
		return
	}
	ctx := r.Context()
	var id string
	err = h.Pool.QueryRow(ctx,
		`INSERT INTO users (email, password_hash, role) VALUES ($1, $2, $3) RETURNING id`,
		req.Email, string(hash), req.Role).Scan(&id)
	if err != nil {
		if strings.Contains(err.Error(), "duplicate key") {
			httpserver.Conflict(w, "email already registered")
			return
		}
		h.Log.Error("admin_user_insert", "error", err)
		httpserver.Internal(w, "database error")
		return
	}

	h.audit(r, "admin.user_create", id, map[string]any{"email": req.Email, "role": req.Role})
	httpserver.JSON(w, http.StatusCreated, map[string]any{
		"id": id, "email": req.Email, "role": req.Role,
	})
}

type updateUserReq struct {
	Role     *string `json:"role"`
	Password *string `json:"password"`
}

// UpdateUser implements PUT /api/v1/admin/users/{id} — change role and/or
// reset the password.
func (h *Handler) UpdateUser(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	var req updateUserReq
	if !httpserver.DecodeJSON(w, r, &req) {
		return
	}
	if req.Role == nil && req.Password == nil {
		httpserver.BadRequest(w, "nothing to update: provide role and/or password", nil)
		return
	}
	ctx := r.Context()

	var currentRole, email string
	err := h.Pool.QueryRow(ctx, `SELECT role, email FROM users WHERE id = $1`, id).
		Scan(&currentRole, &email)
	if errors.Is(err, pgx.ErrNoRows) {
		httpserver.NotFound(w, "user not found")
		return
	}
	if err != nil {
		h.Log.Error("admin_user_get", "error", err)
		httpserver.Internal(w, "database error")
		return
	}

	details := map[string]any{"email": email}
	if req.Role != nil && *req.Role != currentRole {
		if !validRoles[*req.Role] {
			httpserver.BadRequest(w, "role must be 'user' or 'admin'", map[string]any{"role": *req.Role})
			return
		}
		if currentRole == "admin" && *req.Role != "admin" {
			if ok, err := h.otherAdminExists(r, id); err != nil {
				httpserver.Internal(w, "database error")
				return
			} else if !ok {
				httpserver.Conflict(w, "cannot demote the last admin")
				return
			}
		}
		if _, err := h.Pool.Exec(ctx,
			`UPDATE users SET role = $1, updated_at = now() WHERE id = $2`, *req.Role, id); err != nil {
			h.Log.Error("admin_user_role", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		details["role"] = *req.Role
	}
	if req.Password != nil {
		if problems := auth.ValidateRegistration(email, *req.Password); problems != nil {
			httpserver.BadRequest(w, "invalid password", problems)
			return
		}
		hash, err := bcrypt.GenerateFromPassword([]byte(*req.Password), auth.BcryptCost)
		if err != nil {
			h.Log.Error("admin_user_rehash", "error", err)
			httpserver.Internal(w, "hashing error")
			return
		}
		if _, err := h.Pool.Exec(ctx,
			`UPDATE users SET password_hash = $1, updated_at = now() WHERE id = $2`,
			string(hash), id); err != nil {
			h.Log.Error("admin_user_password", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		details["password_reset"] = true
	}

	h.audit(r, "admin.user_update", id, details)
	httpserver.JSON(w, http.StatusOK, map[string]any{"id": id, "updated": true})
}

// DeleteUser implements DELETE /api/v1/admin/users/{id}. Portfolio
// transactions and alerts cascade (schema ON DELETE CASCADE).
func (h *Handler) DeleteUser(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	caller, _ := httpserver.UserFromContext(r.Context())
	if caller.ID == id {
		httpserver.Conflict(w, "you cannot delete your own account")
		return
	}
	ctx := r.Context()

	var role, email string
	err := h.Pool.QueryRow(ctx, `SELECT role, email FROM users WHERE id = $1`, id).
		Scan(&role, &email)
	if errors.Is(err, pgx.ErrNoRows) {
		httpserver.NotFound(w, "user not found")
		return
	}
	if err != nil {
		h.Log.Error("admin_user_get", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	if role == "admin" {
		if ok, err := h.otherAdminExists(r, id); err != nil {
			httpserver.Internal(w, "database error")
			return
		} else if !ok {
			httpserver.Conflict(w, "cannot delete the last admin")
			return
		}
	}

	if _, err := h.Pool.Exec(ctx, `DELETE FROM users WHERE id = $1`, id); err != nil {
		h.Log.Error("admin_user_delete", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	h.audit(r, "admin.user_delete", id, map[string]any{"email": email, "role": role})
	httpserver.JSON(w, http.StatusOK, map[string]any{"id": id, "deleted": true})
}

func (h *Handler) otherAdminExists(r *http.Request, excludeID string) (bool, error) {
	var n int
	err := h.Pool.QueryRow(r.Context(),
		`SELECT count(*) FROM users WHERE role = 'admin' AND id <> $1`, excludeID).Scan(&n)
	if err != nil {
		h.Log.Error("admin_count", "error", err)
	}
	return n > 0, err
}

func (h *Handler) audit(r *http.Request, action, entityID string, details map[string]any) {
	caller, _ := httpserver.UserFromContext(r.Context())
	h.Audit.Record(r.Context(), audit.Entry{
		UserID: &caller.ID, Action: action, Entity: "user", EntityID: entityID,
		Details: details, RequestID: middleware.GetReqID(r.Context()),
	})
}
