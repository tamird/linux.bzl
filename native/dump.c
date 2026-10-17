/* SPDX-License-Identifier: GPL-2.0-only */
/* Prototype frontend linked against the checkout's own Kconfig parser. */
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include "lkc.h"

static unsigned sequence;
static int first = 1;

static void string(const char *s)
{
    const unsigned char *p = (const unsigned char *)(s ? s : "");
    putchar('"');
    for (; *p; p++) {
        switch (*p) {
        case '"': fputs("\\\"", stdout); break;
        case '\\': fputs("\\\\", stdout); break;
        case '\n': fputs("\\n", stdout); break;
        case '\r': fputs("\\r", stdout); break;
        case '\t': fputs("\\t", stdout); break;
        default:
            if (*p < 32) printf("\\u%04x", *p);
            else putchar(*p);
        }
    }
    putchar('"');
}

/* Postfix instructions preserve native expression types without re-parsing. */
static unsigned choice_id(struct menu *menu, struct symbol *sym)
{
    for (; menu; menu = menu->next) {
        if (menu->sym == sym) return (uintptr_t)menu->data;
        unsigned id = choice_id(menu->list, sym);
        if (id) return id;
    }
    return 0;
}

static void code_symbol(struct symbol *sym)
{
    if (!sym->name) {
        unsigned id = choice_id(rootmenu.list, sym);
        if (!id) {
            fputs("Anonymous Kconfig symbol has no syntax entry\n", stderr);
            exit(1);
        }
        printf("[\"sym\",\"choice:%u\"],", id);
    } else {
        printf("[\"%s\",", sym->flags & SYMBOL_CONST ? "lit" : "sym");
        string(sym->name);
        if (sym->flags & SYMBOL_CONST) {
            putchar(','); string(sym_type_name(sym->type));
        }
        fputs("],", stdout);
    }
}

static void code_expr(struct expr *e)
{
    const char *op;
    if (!e) return;
    switch (e->type) {
    case E_SYMBOL: code_symbol(e->left.sym); return;
    case E_NOT:
        code_expr(e->left.expr); fputs("[\"not\"],", stdout); return;
    case E_AND: case E_OR:
        code_expr(e->left.expr); code_expr(e->right.expr);
        op = e->type == E_AND ? "and" : "or"; break;
    case E_EQUAL: case E_UNEQUAL: case E_LTH: case E_LEQ: case E_GTH: case E_GEQ: case E_RANGE:
        code_symbol(e->left.sym); code_symbol(e->right.sym);
        op = e->type == E_EQUAL ? "eq" : e->type == E_UNEQUAL ? "ne" :
             e->type == E_LTH ? "lt" : e->type == E_LEQ ? "le" :
             e->type == E_GTH ? "gt" : e->type == E_GEQ ? "ge" : "range";
        break;
    default: fprintf(stderr, "Unsupported expression type %d\n", e->type); exit(1);
    }
    printf("[\"%s\"],", op);
}

static void bytecode(struct expr *e, const char *fallback)
{
    putchar('[');
    if (e) code_expr(e);
    else printf("[\"lit\",\"%s\",\"tristate\"],", fallback);
    /* A trailing no-op avoids special cases in the streaming writer. */
    fputs("[\"end\"]]", stdout);
}
