package kconfig

import (
	"strings"
	"testing"
)

const legacyChoiceSymbolSource = `
static struct symbol *sym_calc_choice(struct symbol *sym)
{
	sym_calc_visibility(def_sym);
	if (def_sym->visible != no)
		return def_sym;
	return sym_choice_default(sym);
}

void sym_calc_value(struct symbol *sym)
{
	sym->curr = newval;
	if (sym_is_choice(sym) && newval.tri == yes)
		sym->curr.val = sym_calc_choice(sym);
}
`

const memberChoiceSymbolSource = `
struct symbol *sym_calc_choice(struct menu *choice)
{
	sym_calc_visibility(sym);
	if (sym->visible == no)
		continue;
	return sym_choice_default(choice);
}

void sym_calc_value(struct symbol *sym)
{
	choice_menu = sym_get_choice_menu(sym);
	if (choice_menu) {
		sym_calc_choice(choice_menu);
		newval.tri = sym->curr.tri;
	}
}
`

func TestDetectChoiceDialectFromSelectedSymbolSource(t *testing.T) {
	for _, tc := range []struct {
		name   string
		source string
		want   ChoiceDialect
	}{
		{"parent", legacyChoiceSymbolSource, ChoiceDialectParent},
		{"member", memberChoiceSymbolSource, ChoiceDialectMember},
		{"commented competing dialect", memberChoiceSymbolSource + "/* " + legacyChoiceSymbolSource + " */", ChoiceDialectMember},
		{"string competing dialect", memberChoiceSymbolSource + `const char *fake = "sym_calc_choice(sym);";`, ChoiceDialectMember},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := DetectChoiceDialect([]byte(tc.source))
			if err != nil || got != tc.want {
				t.Fatalf("DetectChoiceDialect() = %v, %v; want %v", got, err, tc.want)
			}
		})
	}
}

func TestDetectChoiceDialectRejectsIncompleteAndMixedSource(t *testing.T) {
	for _, tc := range []struct {
		name   string
		source string
	}{
		{"missing", `void sym_calc_value(struct symbol *sym) {}`},
		{"unclosed source comment", legacyChoiceSymbolSource + "/*"},
		{"mixed definitions", legacyChoiceSymbolSource + memberChoiceSymbolSource},
		{"mixed call site", strings.Replace(legacyChoiceSymbolSource, "sym->curr.val = sym_calc_choice(sym);", "sym->curr.val = sym_calc_choice(sym); sym_calc_choice(choice_menu);", 1)},
		{"wrong signature for call site", strings.Replace(legacyChoiceSymbolSource, "struct symbol *sym_calc_choice(struct symbol *sym)", "struct symbol *sym_calc_choice(struct menu *choice)", 1)},
		{"missing member visibility proof", strings.Replace(memberChoiceSymbolSource, "if (sym->visible == no)", "if (sym->visible == yes)", 1)},
		{"missing parent yes guard", strings.Replace(legacyChoiceSymbolSource, "sym_is_choice(sym) && newval.tri == yes", "sym_is_choice(sym)", 1)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := DetectChoiceDialect([]byte(tc.source))
			if got != ChoiceDialectUnknown || err == nil {
				t.Fatalf("DetectChoiceDialect() = %v, %v; want unknown and error", got, err)
			}
		})
	}
}

func TestSetChoiceDialectRejectsUnknownWithoutMutation(t *testing.T) {
	tree := newTree()
	if tree.ChoiceDialect() != ChoiceDialectMember {
		t.Fatal("synthetic Kconfig tree must retain the existing member choice dialect")
	}
	if err := tree.SetChoiceDialect(ChoiceDialectUnknown); err == nil || tree.ChoiceDialect() != ChoiceDialectMember {
		t.Fatalf("SetChoiceDialect(unknown) error=%v, resulting dialect=%v", err, tree.ChoiceDialect())
	}
	if err := tree.SetChoiceDialect(ChoiceDialectParent); err != nil || tree.ChoiceDialect() != ChoiceDialectParent {
		t.Fatalf("SetChoiceDialect(parent) error=%v, resulting dialect=%v", err, tree.ChoiceDialect())
	}
}
