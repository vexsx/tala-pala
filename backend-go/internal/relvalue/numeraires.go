package relvalue

// GET /api/v1/markets/numeraires -- the units of account this deployment can
// actually express a value in, each with the market series that performs the
// conversion and that series' real coverage.
//
// It exists so the UI never offers a choice that would come back as a page of
// nulls. A numeraire dropdown built from a hard-coded list is a promise the
// server may not be able to keep: GOLD terms require IR_GOLD_18K to have been
// collected, and on a deployment where it has not been, every figure behind
// that menu item is null. Here the menu is built from what is stored, and a
// numeraire that cannot be served says so, with the reason, instead of being
// quietly absent -- an operator debugging a missing option needs to see it
// listed as unavailable, not to find nothing at all.

import (
	"fmt"
	"net/http"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// numeraireItem is one offered (or refused) unit of account.
//
// The identifier on the wire is `key`, the backing instrument is `series`, and
// `notes` is an ARRAY -- a menu built against `code`, `series_code` or a scalar
// `note` is reading fields this endpoint has never emitted. Every <option> a
// client renders must carry an explicit value of item.key: an <option> with no
// value attribute falls back to its own visible text, which is how a dropdown
// ends up requesting numeraire=US%20dollar.
type numeraireItem struct {
	numeraireBlock
	// LabelEN and LabelFA are the menu labels. They repeat name_en/name_fa,
	// which stay exactly as they have always been emitted: the display contract
	// names these fields label_*, the wire has always carried name_*, and a
	// dropdown that renders `undefined` because the two disagree is a worse
	// outcome than two spellings of one string.
	LabelEN string `json:"label_en"`
	LabelFA string `json:"label_fa"`
	// Available is what a client should gate its menu on. False means a request
	// naming this numeraire will be refused with a 400.
	Available bool `json:"available"`
	// UnavailableReason is null when Available. It names the missing series
	// rather than saying "not configured", because the fix is an ingest, not a
	// setting.
	UnavailableReason *string `json:"unavailable_reason"`
	// IsIdentity marks the hub: IRT converts an IRT-quoted asset by doing
	// nothing to it, so it needs no backing series and cannot become
	// unavailable.
	IsIdentity bool `json:"is_identity"`
}

type numeraireListResponse struct {
	AsOf  time.Time       `json:"as_of"`
	Items []numeraireItem `json:"items"`
	Count int             `json:"count"`
	// Default is the numeraire a client should select when it has no stored
	// preference. It is IRT, and it is safe to fall back to unconditionally:
	// the hub is the identity conversion, needs no backing series and is the
	// one entry that can never become unavailable. It is published rather than
	// left for the client to hard-code, because a fallback compiled into the
	// page is a second copy of this package's default that nothing keeps in
	// step with parseNumeraire.
	Default  string   `json:"default"`
	Warnings []string `json:"warnings"`
}

// buildNumeraireList projects the numeraire vocabulary onto what is stored.
// Pure function (unit tested).
func buildNumeraireList(instruments []instrumentRow, series map[string]dailySeries,
	asOf time.Time) numeraireListResponse {
	out := numeraireListResponse{
		AsOf:     asOf.UTC(),
		Items:    make([]numeraireItem, 0, len(numeraireSpecs)),
		Default:  defaultNumeraire,
		Warnings: []string{},
	}
	for _, spec := range numeraireSpecs {
		item := numeraireItem{
			numeraireBlock: buildNumeraireBlock(spec, instruments, series),
			LabelEN:        spec.NameEN,
			LabelFA:        spec.NameFA,
			Available:      true,
			IsIdentity:     spec.Series == "",
		}
		if spec.Series != "" && len(series[spec.Series]) == 0 {
			item.Available = false
			reason := fmt.Sprintf(
				"%s carries no stored observation in this deployment, so no value can be "+
					"converted into %s.", spec.Series, spec.Key)
			item.UnavailableReason = &reason
		}
		out.Items = append(out.Items, item)
	}
	out.Count = len(out.Items)

	// The hub itself never becomes unavailable, but half of what it converts
	// does: a USD-quoted asset reaches toman through USD_IRT, so its absence
	// makes IRT a partial answer rather than a broken one. That distinction is
	// a warning, not an availability flag.
	if len(series[usdSeriesCode]) == 0 {
		out.Warnings = append(out.Warnings, fmt.Sprintf(
			"%s carries no stored observation: IRT is still offered (an IRT-quoted asset "+
				"needs no conversion) but USD-quoted instruments cannot be expressed in "+
				"toman, and their IRT figures will be null with a note.", usdSeriesCode))
	}
	return out
}

// Numeraires implements GET /api/v1/markets/numeraires.
func (h *Handler) Numeraires(w http.ResponseWriter, r *http.Request) {
	now := time.Now().UTC()
	ctx := r.Context()

	instruments, err := h.loadInstruments(ctx)
	if err != nil {
		h.Log.Error("numeraires_instruments", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	series, err := h.loadDailySeries(ctx, conversionSeriesCodes, now)
	if err != nil {
		h.Log.Error("numeraires_series", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, buildNumeraireList(instruments, series, now))
}
