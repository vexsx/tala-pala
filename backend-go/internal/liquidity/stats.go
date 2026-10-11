package liquidity

// The three statistics the backtest needs, written out because the module
// carries no numerics library: an empirical quantile, the Diebold-Mariano test
// with the Harvey-Leybourne-Newbold small-sample correction, and the Student t
// distribution function that test's p-value is read from.
//
// Each one is the textbook definition with the same conventions the research
// used (numpy's default quantile, scipy's t distribution), so the acceptance
// test can compare p-values and band edges digit for digit.

import (
	"math"
	"sort"
)

// quantile is the empirical p-quantile with linear interpolation between order
// statistics (Hyndman & Fan type 7, numpy's default): position (n-1)p in the
// sorted sample. The input is not modified. NaN for an empty sample.
func quantile(xs []float64, p float64) float64 {
	if len(xs) == 0 {
		return math.NaN()
	}
	s := append([]float64(nil), xs...)
	sort.Float64s(s)
	return sortedQuantile(s, p)
}

// sortedQuantile is quantile over an already sorted sample.
func sortedQuantile(s []float64, p float64) float64 {
	if len(s) == 0 {
		return math.NaN()
	}
	pos := float64(len(s)-1) * p
	lo := int(math.Floor(pos))
	if lo >= len(s)-1 {
		return s[len(s)-1]
	}
	return s[lo] + (pos-float64(lo))*(s[lo+1]-s[lo])
}

// dmResult is one Diebold-Mariano comparison.
type dmResult struct {
	// Stat is the HLN-corrected statistic; negative means the first model's
	// loss was smaller.
	Stat float64
	// P is the ONE-SIDED p-value for "the first model's loss is smaller",
	// read from Student's t with n-1 degrees of freedom.
	P  float64
	N  int
	OK bool
}

// dieboldMariano tests whether the loss differential d (model loss minus
// benchmark loss, one value per forecast origin, in origin order) has a
// negative mean, for h-step forecasts.
//
// h-step forecasts made one month apart overlap, so their errors are
// autocorrelated up to lag h-1; the long-run variance is the Bartlett-weighted
// sum of the first h autocovariances (Diebold & Mariano 1995), falling back to
// the plain variance when the weighted sum is not positive. Harvey, Leybourne
// and Newbold (1997) scale the statistic by sqrt((n+1-2h+h(h-1)/n)/n) and read
// it against Student's t with n-1 degrees of freedom, which is what makes it
// usable at 24 months with ~5 independent outcomes.
//
// Not OK -- no p-value -- when n <= 2h (too few origins for the lag window) or
// when the two models' losses are identical everywhere.
func dieboldMariano(d []float64, h int) dmResult {
	n := len(d)
	out := dmResult{N: n}
	if h < 1 || n <= 2*h {
		return out
	}
	allZero := true
	var mean float64
	for _, v := range d {
		mean += v
		if math.Abs(v) > 1e-8 {
			allZero = false
		}
	}
	if allZero {
		return out
	}
	mean /= float64(n)
	gamma := make([]float64, h)
	for k := 0; k < h; k++ {
		var s float64
		for i := k; i < n; i++ {
			s += (d[i] - mean) * (d[i-k] - mean)
		}
		gamma[k] = s / float64(n)
	}
	v := gamma[0]
	for k := 1; k < h; k++ {
		v += 2 * (1 - float64(k)/float64(h)) * gamma[k]
	}
	if v <= 0 {
		v = gamma[0]
	}
	if v <= 0 {
		return out
	}
	nf, hf := float64(n), float64(h)
	dm := mean / math.Sqrt(v/nf)
	hln := dm * math.Sqrt((nf+1-2*hf+hf*(hf-1)/nf)/nf)
	out.Stat = hln
	out.P = studentTCDF(hln, nf-1)
	out.OK = !math.IsNaN(out.P)
	return out
}

// studentTCDF is P(T <= x) for Student's t with nu degrees of freedom:
// 1 - I_z(nu/2, 1/2)/2 for x >= 0 and I_z(nu/2, 1/2)/2 below, with
// z = nu/(nu+x^2) and I the regularised incomplete beta function.
func studentTCDF(x, nu float64) float64 {
	if math.IsNaN(x) || nu <= 0 {
		return math.NaN()
	}
	if math.IsInf(x, 1) {
		return 1
	}
	if math.IsInf(x, -1) {
		return 0
	}
	tail := 0.5 * regIncBeta(nu/2, 0.5, nu/(nu+x*x))
	if x >= 0 {
		return 1 - tail
	}
	return tail
}

// regIncBeta is the regularised incomplete beta function I_x(a, b), evaluated
// from its continued fraction (DLMF 8.17.22) by the modified Lentz method. The
// fraction converges fast for x < (a+1)/(a+b+2); above that the symmetry
// I_x(a,b) = 1 - I_{1-x}(b,a) is used so it is always evaluated where it
// converges.
func regIncBeta(a, b, x float64) float64 {
	switch {
	case x <= 0:
		return 0
	case x >= 1:
		return 1
	}
	la, _ := math.Lgamma(a)
	lb, _ := math.Lgamma(b)
	lab, _ := math.Lgamma(a + b)
	front := math.Exp(a*math.Log(x) + b*math.Log1p(-x) - (la + lb - lab))
	if x < (a+1)/(a+b+2) {
		return front * betaFraction(a, b, x) / a
	}
	return 1 - front*betaFraction(b, a, 1-x)/b
}

// betaFraction evaluates 1/(1+ d1/(1+ d2/(1+ ...))) with
//
//	d(2m+1) = -(a+m)(a+b+m)x / ((a+2m)(a+2m+1))
//	d(2m)   =  m(b-m)x       / ((a+2m-1)(a+2m))
//
// by the modified Lentz recurrence, stopping when a step changes the value by
// less than one part in 1e15.
func betaFraction(a, b, x float64) float64 {
	const (
		tiny    = 1e-300
		eps     = 1e-15
		maxIter = 500
	)
	c := 1.0
	d := 1 - (a+b)*x/(a+1)
	if math.Abs(d) < tiny {
		d = tiny
	}
	d = 1 / d
	f := d
	for m := 1; m <= maxIter; m++ {
		mf := float64(m)
		// Even step.
		num := mf * (b - mf) * x / ((a + 2*mf - 1) * (a + 2*mf))
		d = 1 + num*d
		if math.Abs(d) < tiny {
			d = tiny
		}
		c = 1 + num/c
		if math.Abs(c) < tiny {
			c = tiny
		}
		d = 1 / d
		f *= d * c
		// Odd step.
		num = -(a + mf) * (a + b + mf) * x / ((a + 2*mf) * (a + 2*mf + 1))
		d = 1 + num*d
		if math.Abs(d) < tiny {
			d = tiny
		}
		c = 1 + num/c
		if math.Abs(c) < tiny {
			c = tiny
		}
		d = 1 / d
		step := d * c
		f *= step
		if math.Abs(step-1) < eps {
			break
		}
	}
	return f
}
