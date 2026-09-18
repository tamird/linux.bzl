package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/hermeticbuild/linux.bzl/internal/kconfig"
	"github.com/hermeticbuild/linux.bzl/internal/toolaction"
)

func TestNativeConfigBindsDeclaredRelativeSourceRoots(t *testing.T) {
	execroot, work := t.TempDir(), t.TempDir()
	const alias = "external/declared-source/library"
	const physical = "mapped-inputs/source"
	if err := os.MkdirAll(filepath.Join(execroot, physical, "core/src"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(execroot, physical, "core/src/lib.rs"), []byte("core source"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := bindNativeConfigSourceRoots(work, execroot, map[string]string{alias: toolaction.ExecutionRootMarker + "/" + physical}); err != nil {
		t.Fatal(err)
	}
	contents, err := os.ReadFile(filepath.Join(work, alias, "core/src/lib.rs"))
	if err != nil || string(contents) != "core source" {
		t.Fatalf("native private-tree source read = %q, %v", contents, err)
	}
	// A nested alias must not traverse the first immutable source and create
	// files there, even when both aliases name declared inputs.
	if err := bindNativeConfigSourceRoots(work, execroot, map[string]string{alias + "/nested": physical}); err == nil {
		t.Fatal("native source binding accepted a nested immutable source alias")
	}
}

func TestReadNativeConfigProjectionPreservesSourceArtifacts(t *testing.T) {
	root := t.TempDir()
	files := map[string]string{
		".config": "CONFIG_PRESENT=y\n", ".config.old": "seed\n",
		"include/config/auto.conf":     "CONFIG_PRESENT=y\n",
		"include/config/auto.conf.cmd": "deps_config := Kconfig\n",
		"include/generated/autoconf.h": "#define CONFIG_PRESENT 1\n",
		"include/config/PRESENT":       "", "include/config/cc/version/text.h": "",
		".tools/runtime": "private\n", "unrelated": "private\n",
	}
	for path, content := range files {
		filename := filepath.Join(root, path)
		if err := os.MkdirAll(filepath.Dir(filename), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filename, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink("missing-source", filepath.Join(root, kbuildEvalSourceTree)); err != nil {
		t.Fatal(err)
	}
	projection, err := readNativeConfigProjection(root)
	if err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{".config.old", ".tools/runtime", "unrelated", "include/generated/rustc_cfg"} {
		if _, exists := projection.files[path]; exists {
			t.Errorf("projection includes non-native artifact %q", path)
		}
	}
	for _, path := range []string{"include/config/PRESENT", "include/config/cc/version/text.h"} {
		if value, exists := projection.files[path]; !exists || value != "" {
			t.Errorf("native dependency marker %q = (%q, %t), want present and empty", path, value, exists)
		}
	}
}

func TestNativeConfigChecksExportedValues(t *testing.T) {
	tree, err := kconfig.Parse(t.Context(), strings.NewReader(`
config ENABLED
	bool
config DISABLED
	bool
config HIDDEN
	bool
config EMPTY
	string
config NUMBER
	hex
`), "Kconfig", kconfig.Options{})
	if err != nil {
		t.Fatal(err)
	}
	resolved := &kconfig.ResolvedConfig{
		Effective: map[string]string{"CONFIG_ENABLED": "y", "CONFIG_DISABLED": "n", "CONFIG_HIDDEN": "n", "CONFIG_EMPTY": `""`, "CONFIG_NUMBER": "0x00a"},
		Written:   map[string]bool{"CONFIG_ENABLED": true, "CONFIG_EMPTY": true, "CONFIG_NUMBER": true},
	}
	canonical := "CONFIG_ENABLED=y\n# CONFIG_DISABLED is not set\nCONFIG_EMPTY=\"\"\nCONFIG_NUMBER=0x00a\n"
	for _, test := range []struct {
		name      string
		config    string
		wantError string
	}{
		{name: "native visibility omits hidden disabled", config: canonical},
		{name: "absent disabled", config: strings.ReplaceAll(canonical, "# CONFIG_DISABLED is not set\n", "")},
		{name: "missing empty string", config: strings.ReplaceAll(canonical, "CONFIG_EMPTY=\"\"\n", ""), wantError: "CONFIG_EMPTY"},
		{name: "additional enabled symbol", config: canonical + "CONFIG_HIDDEN=y\n", wantError: "CONFIG_HIDDEN"},
		{name: "changed numeric spelling", config: strings.ReplaceAll(canonical, "0x00a", "0xA"), wantError: "CONFIG_NUMBER"},
		{name: "changed value", config: strings.ReplaceAll(canonical, "CONFIG_ENABLED=y", "# CONFIG_ENABLED is not set"), wantError: "CONFIG_ENABLED"},
	} {
		t.Run(test.name, func(t *testing.T) {
			projection := &nativeConfigProjection{files: map[string]string{".config": test.config}}
			err := projection.verify(tree, resolved)
			if test.wantError == "" && err != nil {
				t.Fatal(err)
			}
			if test.wantError != "" && (err == nil || !strings.Contains(err.Error(), test.wantError)) {
				t.Fatalf("verify() error = %v, want %s disagreement", err, test.wantError)
			}
		})
	}
}

// These documents are source-shaped inputs to planner unit tests. The native
// serializer itself is exercised by real-source Bazel actions.
func nativeConfigFixtureForTest(config, autoConf, autoconf string) map[string]string {
	return map[string]string{
		".config":                      config,
		"include/config/auto.conf":     autoConf,
		"include/config/auto.conf.cmd": "include/config/auto.conf: Kconfig\n",
		"include/generated/autoconf.h": autoconf,
	}
}

func TestDeclaredNativeConfigTreeRootSymlink(t *testing.T) {
	root := t.TempDir()
	for name, contents := range nativeConfigFixtureForTest("CONFIG_TEST=y\n", "CONFIG_TEST=y\n", "#define CONFIG_TEST 1\n") {
		filename := filepath.Join(root, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(filename), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filename, []byte(contents), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	alias := filepath.Join(t.TempDir(), "declared-tree")
	if err := os.Symlink(root, alias); err != nil {
		t.Fatal(err)
	}
	if _, err := readDeclaredNativeConfigProjection(alias); err != nil {
		t.Fatal(err)
	}
	if _, err := readNativeConfigProjection(alias); err == nil {
		t.Fatal("serializer output audit accepted a symlink root")
	}
}
