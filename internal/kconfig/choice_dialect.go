package kconfig

import (
	"fmt"
	"regexp"
	"strings"
)

// ChoiceDialect describes the choice calculation selected by
// scripts/kconfig/symbol.c, independently of the kernel's release number.
type ChoiceDialect uint8

const (
	ChoiceDialectUnknown ChoiceDialect = iota
	// ChoiceDialectParent calculates the choice when the parent reaches yes.
	ChoiceDialectParent
	// ChoiceDialectMember calculates a choice while resolving its members.
	ChoiceDialectMember
)

func (t *Tree) SetChoiceDialect(dialect ChoiceDialect) error {
	if t == nil {
		return fmt.Errorf("set choice dialect on a nil Kconfig tree")
	}
	if dialect != ChoiceDialectParent && dialect != ChoiceDialectMember {
		return fmt.Errorf("set choice dialect: unsupported dialect %d", dialect)
	}
	t.choiceDialect = dialect
	return nil
}

func (t *Tree) ChoiceDialect() ChoiceDialect {
	if t == nil {
		return ChoiceDialectUnknown
	}
	return t.choiceDialect
}

var (
	choiceCalculationDefinition = regexp.MustCompile(`\b(?:static\s+)?struct\s+symbol\s*\*\s*sym_calc_choice\s*\(\s*struct\s+(symbol|menu)\s*\*\s*(sym|choice)\s*\)\s*\{`)
	choiceValueDefinition       = regexp.MustCompile(`\bvoid\s+sym_calc_value\s*\(\s*struct\s+symbol\s*\*\s*sym\s*\)\s*\{`)
	choiceFunctionCall          = regexp.MustCompile(`\bsym_calc_choice\s*\(\s*[a-zA-Z_][a-zA-Z_0-9]*\s*\)\s*;`)
	choiceParentCall            = regexp.MustCompile(`\bif\s*\(\s*sym_is_choice\s*\(\s*sym\s*\)\s*&&\s*newval\s*\.\s*tri\s*==\s*yes\s*\)\s*sym\s*->\s*curr\s*\.\s*val\s*=\s*sym_calc_choice\s*\(\s*sym\s*\)\s*;`)
	choiceMemberCall            = regexp.MustCompile(`\bif\s*\(\s*choice_menu\s*\)\s*\{\s*sym_calc_choice\s*\(\s*choice_menu\s*\)\s*;\s*newval\s*\.\s*tri\s*=\s*sym\s*->\s*curr\s*\.\s*tri\s*;\s*\}`)
	choiceParentVisibility      = regexp.MustCompile(`\bsym_calc_visibility\s*\(\s*def_sym\s*\)\s*;[\s\S]*\bdef_sym\s*->\s*visible\s*!=\s*no`)
	choiceMemberVisibility      = regexp.MustCompile(`\bsym_calc_visibility\s*\(\s*sym\s*\)\s*;\s*if\s*\(\s*sym\s*->\s*visible\s*==\s*no\s*\)`)
)

// DetectChoiceDialect examines the exact selected scripts/kconfig/symbol.c
// bytes supplied by the source owner. Comments and C string literals cannot
// claim a dialect. A definition without its corresponding sym_calc_value()
// call site, or competing call sites, cannot authorize choice semantics.
func DetectChoiceDialect(source []byte) (ChoiceDialect, error) {
	code, err := choiceCodeWithoutCommentsAndLiterals(source)
	if err != nil {
		return ChoiceDialectUnknown, fmt.Errorf("scripts/kconfig/symbol.c: %w", err)
	}
	definitions := choiceCalculationDefinition.FindAllStringSubmatchIndex(code, -1)
	if len(definitions) != 1 {
		return ChoiceDialectUnknown, fmt.Errorf("scripts/kconfig/symbol.c: expected one supported sym_calc_choice definition, found %d", len(definitions))
	}
	valueDefinitions := choiceValueDefinition.FindAllStringIndex(code, -1)
	if len(valueDefinitions) != 1 {
		return ChoiceDialectUnknown, fmt.Errorf("scripts/kconfig/symbol.c: expected one sym_calc_value definition, found %d", len(valueDefinitions))
	}
	valueBody, err := choiceCFunctionBody(code, valueDefinitions[0][1])
	if err != nil {
		return ChoiceDialectUnknown, fmt.Errorf("scripts/kconfig/symbol.c: sym_calc_value: %w", err)
	}
	calculationBody, err := choiceCFunctionBody(code, definitions[0][1])
	if err != nil {
		return ChoiceDialectUnknown, fmt.Errorf("scripts/kconfig/symbol.c: sym_calc_choice: %w", err)
	}
	// Every call must be in sym_calc_value's selected calculation branch.
	if calls := choiceFunctionCall.FindAllStringIndex(code, -1); len(calls) != 1 ||
		calls[0][0] < valueDefinitions[0][1] || calls[0][1] > valueDefinitions[0][1]+len(valueBody) {
		return ChoiceDialectUnknown, fmt.Errorf("scripts/kconfig/symbol.c: expected one sym_calc_choice call in sym_calc_value")
	}
	signature := code[definitions[0][2]:definitions[0][3]]
	argument := code[definitions[0][4]:definitions[0][5]]
	switch {
	case signature == "symbol" && argument == "sym" && choiceParentCall.MatchString(valueBody) &&
		choiceParentVisibility.MatchString(calculationBody) && !choiceMemberCall.MatchString(valueBody):
		return ChoiceDialectParent, nil
	case signature == "menu" && argument == "choice" && choiceMemberCall.MatchString(valueBody) &&
		choiceMemberVisibility.MatchString(calculationBody) && !choiceParentCall.MatchString(valueBody):
		return ChoiceDialectMember, nil
	default:
		return ChoiceDialectUnknown, fmt.Errorf("scripts/kconfig/symbol.c: sym_calc_choice definition and sym_calc_value call site do not establish one supported choice dialect")
	}
}

func choiceCFunctionBody(code string, start int) (string, error) {
	depth := 1
	for i := start; i < len(code); i++ {
		switch code[i] {
		case '{':
			depth++
		case '}':
			depth--
			if depth == 0 {
				return code[start:i], nil
			}
		}
	}
	return "", fmt.Errorf("unclosed function body")
}

func choiceCodeWithoutCommentsAndLiterals(source []byte) (string, error) {
	var out strings.Builder
	out.Grow(len(source))
	const (
		code byte = iota
		lineComment
		blockComment
		stringLiteral
		charLiteral
	)
	state := code
	for i := 0; i < len(source); i++ {
		c := source[i]
		next := byte(0)
		if i+1 < len(source) {
			next = source[i+1]
		}
		switch state {
		case code:
			switch {
			case c == '/' && next == '/':
				state = lineComment
				out.WriteString("  ")
				i++
			case c == '/' && next == '*':
				state = blockComment
				out.WriteString("  ")
				i++
			case c == '"':
				state = stringLiteral
				out.WriteByte(' ')
			case c == '\'':
				state = charLiteral
				out.WriteByte(' ')
			default:
				out.WriteByte(c)
			}
		case lineComment:
			if c == '\n' {
				state = code
				out.WriteByte('\n')
			} else {
				out.WriteByte(' ')
			}
		case blockComment:
			if c == '*' && next == '/' {
				state = code
				out.WriteString("  ")
				i++
			} else if c == '\n' {
				out.WriteByte('\n')
			} else {
				out.WriteByte(' ')
			}
		case stringLiteral, charLiteral:
			if c == '\\' && i+1 < len(source) {
				out.WriteString("  ")
				i++
			} else if (state == stringLiteral && c == '"') || (state == charLiteral && c == '\'') {
				state = code
				out.WriteByte(' ')
			} else if c == '\n' {
				return "", fmt.Errorf("unterminated C literal")
			} else {
				out.WriteByte(' ')
			}
		}
	}
	if state == blockComment || state == stringLiteral || state == charLiteral {
		return "", fmt.Errorf("unterminated C comment or literal")
	}
	return out.String(), nil
}
