package capabilities_test

import (
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"path/filepath"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// Only agent.update may return jobs.ErrDeferred (spec §3.2). The runner turns
// a deferral from any other type into `failed`; this keeps a second capability
// from even trying, by refusing any reference to the sentinel outside
// selfupdate.
func TestOnlySelfUpdateReferencesErrDeferred(t *testing.T) {
	if jobs.DeferrableJobType != "agent.update" {
		t.Fatalf("DeferrableJobType = %q", jobs.DeferrableJobType)
	}
	var offenders []string
	err := filepath.WalkDir(".", func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		if strings.HasPrefix(filepath.ToSlash(path), "selfupdate/") {
			return nil
		}
		f, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
		if err != nil {
			return err
		}
		ast.Inspect(f, func(n ast.Node) bool {
			if sel, ok := n.(*ast.SelectorExpr); ok && sel.Sel.Name == "ErrDeferred" {
				offenders = append(offenders, path)
			}
			return true
		})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(offenders) > 0 {
		t.Fatalf("jobs.ErrDeferred is referenced outside selfupdate: %v", offenders)
	}
}
