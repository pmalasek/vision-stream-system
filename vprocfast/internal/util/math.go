package util

// MaxInt vrací větší ze dvou integer hodnot.
func MaxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
