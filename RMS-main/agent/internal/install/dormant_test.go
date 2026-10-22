package install

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"strings"
	"testing"
)

// IDLE BUDGET (spec §5 Part 1): the service never initialises this package.
// Go initialises every linked package at start-up, so "never initialises"
// can only be true if initialising it costs nothing: no init(), and no
// package-level variable whose value is a call (a regexp.MustCompile here
// would compile in every service process for the life of the machine).
// Constants and composite literals are free. The service path never CALLS in
// either: cmd/agent's installerRequested answers without this package.
func TestThisPackageHasNothingToInitialise(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}
	fset := token.NewFileSet()
	checked := 0
	scanned := map[string]bool{}
	for _, e := range entries {
		name := e.Name()
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		scanned[name] = true
		f, err := parser.ParseFile(fset, name, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		checked++
		for _, decl := range f.Decls {
			switch d := decl.(type) {
			case *ast.FuncDecl:
				if d.Recv == nil && d.Name.Name == "init" {
					t.Errorf("%s declares init()", name)
				}
			case *ast.GenDecl:
				if d.Tok != token.VAR {
					continue
				}
				for _, spec := range d.Specs {
					for _, v := range spec.(*ast.ValueSpec).Values {
						ast.Inspect(v, func(n ast.Node) bool {
							if _, ok := n.(*ast.CallExpr); ok {
								t.Errorf("%s: a package-level variable is initialised by a call", name)
								return false
							}
							return true
						})
					}
				}
			}
		}
	}
	if checked < 5 {
		t.Fatalf("checked %d files; the scan is not looking at this package", checked)
	}
	// The Group Policy MSI's mode (GPO MSI spec 2026-09-30 §2.3) is held to
	// the same rule, on every OS it builds for: named, so a rename cannot
	// quietly take it out of the scan.
	for _, name := range []string{"msi.go", "msi_windows.go", "msi_other.go"} {
		if !scanned[name] {
			t.Errorf("%s was not scanned; the MSI mode must have nothing to initialise either", name)
		}
	}
}
