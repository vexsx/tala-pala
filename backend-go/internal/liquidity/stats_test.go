package liquidity

import (
	"math"
	"testing"
)

func TestQuantileIsNumpysLinearRule(t *testing.T) {
	xs := []float64{4, 1, 3, 2, 5} // sorted: 1 2 3 4 5
	cases := map[float64]float64{0: 1, 1: 5, 0.5: 3, 0.25: 2, 0.05: 1.2, 0.95: 4.8}
	for p, want := range cases {
		if got := quantile(xs, p); math.Abs(got-want) > 1e-12 {
			t.Errorf("quantile(%v) = %v, want %v", p, got, want)
		}
	}
	if xs[0] != 4 {
		t.Error("quantile must not reorder its input")
	}
	if !math.IsNaN(quantile(nil, 0.5)) {
		t.Error("the quantile of nothing is not a number")
	}
	// Even count: the median is the mean of the two middle values.
	if got := quantile([]float64{1, 2, 3, 10}, 0.5); got != 2.5 {
		t.Errorf("median = %v, want 2.5", got)
	}
}

// Reference values from the closed forms: with one degree of freedom t is
// Cauchy, F(x) = 1/2 + atan(x)/pi; with two, F(x) = 1/2 + x/(2 sqrt(2+x^2)).
// The others are standard table values.
func TestStudentTCDF(t *testing.T) {
	for _, x := range []float64{-5, -1.3, -0.2, 0, 0.7, 2.5, 12} {
		near(t, "t1 cdf", studentTCDF(x, 1), 0.5+math.Atan(x)/math.Pi, 1e-12)
		near(t, "t2 cdf", studentTCDF(x, 2), 0.5+x/(2*math.Sqrt(2+x*x)), 1e-12)
	}
	near(t, "t10(2.228)", studentTCDF(2.228, 10), 0.975, 5e-5)
	near(t, "t30(1.697)", studentTCDF(1.697, 30), 0.95, 5e-5)
	near(t, "t120(-1.980)", studentTCDF(-1.980, 120), 0.025, 5e-5)
	near(t, "large df approaches the normal", studentTCDF(1.959964, 1e6), 0.975, 1e-5)
	if studentTCDF(math.Inf(1), 5) != 1 || studentTCDF(math.Inf(-1), 5) != 0 {
		t.Error("infinite tails")
	}
}

func TestRegIncBetaSymmetryAndEdges(t *testing.T) {
	for _, c := range [][3]float64{{2, 3, 0.4}, {0.5, 0.5, 0.9}, {60, 0.5, 0.97}, {5, 1, 0.3}} {
		a, b, x := c[0], c[1], c[2]
		near(t, "symmetry", regIncBeta(a, b, x)+regIncBeta(b, a, 1-x), 1, 1e-12)
	}
	// I_x(a, 1) = x^a.
	near(t, "I_x(a,1)", regIncBeta(5, 1, 0.3), math.Pow(0.3, 5), 1e-14)
	if regIncBeta(2, 3, 0) != 0 || regIncBeta(2, 3, 1) != 1 {
		t.Error("edges")
	}
}

func TestDieboldMariano(t *testing.T) {
	// A model whose loss is smaller every time: strongly significant.
	d := make([]float64, 60)
	for i := range d {
		d[i] = -0.01 - 0.001*float64(i%5)
	}
	r := dieboldMariano(d, 3)
	if !r.OK || r.Stat >= 0 || r.P > 0.001 {
		t.Fatalf("uniformly better model: %+v", r)
	}
	// Symmetric differences: no evidence either way.
	for i := range d {
		d[i] = 0.01 * float64(1-2*(i%2))
	}
	r = dieboldMariano(d, 1)
	if !r.OK || math.Abs(r.P-0.5) > 0.01 {
		t.Fatalf("balanced losses: %+v", r)
	}
	// Too few origins for the lag window, and identical losses, are refused.
	if r := dieboldMariano(make([]float64, 24), 12); r.OK {
		t.Fatal("n <= 2h must not produce a p-value")
	}
	if r := dieboldMariano(make([]float64, 100), 3); r.OK {
		t.Fatal("identical losses must not produce a p-value")
	}
}

// The HLN statistic by hand for a short series, h=2: mean -0.5, gamma0 1.25,
// gamma1 -0.71875 -> v = 1.25 + 2*0.5*(-0.71875) = 0.53125,
// DM = -0.5/sqrt(0.53125/8), HLN = DM * sqrt((8+1-4+2/8)/8).
func TestDieboldMarianoByHand(t *testing.T) {
	d := []float64{-2, 1, -1, 0, -2, 1, 0, -1}
	r := dieboldMariano(d, 2)
	mean := -0.5
	var g0, g1 float64
	for i, v := range d {
		g0 += (v - mean) * (v - mean)
		if i >= 1 {
			g1 += (v - mean) * (d[i-1] - mean)
		}
	}
	g0, g1 = g0/8, g1/8
	if g0 != 1.25 || g1 != -0.71875 {
		t.Fatalf("autocovariances %v, %v", g0, g1)
	}
	v := g0 + 2*0.5*g1
	want := mean / math.Sqrt(v/8) * math.Sqrt((8+1-4+2.0/8)/8)
	near(t, "HLN statistic", r.Stat, want, 1e-12)
	near(t, "p", r.P, studentTCDF(want, 7), 1e-12)
}
