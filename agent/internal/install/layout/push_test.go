package layout

import "testing"

func TestCheckEnrollOriginAcceptsABareHTTPSOrigin(t *testing.T) {
	for _, ok := range []string{
		"https://itms.example.com",
		"https://itms.example.com:8443",
		"https://localhost:43121",
		"https://10.0.0.5",
	} {
		if err := CheckEnrollOrigin(ok); err != nil {
			t.Errorf("CheckEnrollOrigin(%q) = %v", ok, err)
		}
	}
}

func TestCheckEnrollOriginRefusesAnythingElse(t *testing.T) {
	for _, bad := range []string{
		"",
		"http://itms.example.com",
		"itms.example.com",
		"https://",
		"https://itms.example.com/",
		"https://itms.example.com/enroll/v1",
		"https://itms.example.com?x=1",
		"https://user@itms.example.com",
		"https://[::1]:8443",
		"https://itms.example.com:",
		"https://itms.example.com:0",
		"https://itms.example.com:65536",
		"https://itms.example.com:12a",
		"https://itms example.com",
		"https://itms.example.com';id;'",
		"https://-itms.example.com",
		"https://itms.example.com\n",
	} {
		if err := CheckEnrollOrigin(bad); err == nil {
			t.Errorf("CheckEnrollOrigin(%q) accepted", bad)
		}
	}
}
