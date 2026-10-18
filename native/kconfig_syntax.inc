/* File-local syntax export. Source statements are entries, not file reads. */
#include <string.h>

static void syntax_ids(struct menu *menu)
{
    for (; menu; menu = menu->next) {
        menu->data = (void *)(uintptr_t)++sequence;
        if (menu->list) syntax_ids(menu->list);
    }
}

static void syntax_walk(struct menu *menu, unsigned parent)
{
    for (; menu; menu = menu->next) {
        struct symbol *sym = menu->sym;
        unsigned id = (uintptr_t)menu->data;
        /* Only the injected source entry has help without a symbol. Native
         * comments accept dependencies, but not a help statement. */
        int source = !sym && menu->help;
        int transitional = 0;
        int allnoconfig_y = 0;
        int no_write = 0;
#ifdef SYMBOL_TRANS
        transitional = sym && (sym->flags & SYMBOL_TRANS);
#endif
#ifdef SYMBOL_ALLNOCONFIG_Y
        allnoconfig_y = sym && (sym->flags & SYMBOL_ALLNOCONFIG_Y);
#endif
#ifdef SYMBOL_NO_WRITE
        no_write = sym && (sym->flags & SYMBOL_NO_WRITE);
#endif
        if (!first) putchar(',');
        first = 0;
        printf("{\"id\":%u,\"parent\":%u,\"kind\":", id, parent);
        string(source ? "source" :
               sym && sym_is_choice(sym) ? "choice" : sym ? "config" : "scope");
        fputs(",\"symbol\":", stdout); string(sym ? sym->name : "");
        fputs(",\"type\":", stdout); string(sym ? sym_type_name(sym->type) : "unknown");
        fputs(",\"source\":", stdout); string(prototype_menu_filename(menu));
        printf(",\"line\":%d,\"modules\":%s,\"transitional\":%s,\"allnoconfig_y\":%s,\"no_write\":%s", menu->lineno,
               sym && sym == modules_sym ? "true" : "false",
               transitional ? "true" : "false",
               allnoconfig_y ? "true" : "false",
               no_write ? "true" : "false");
        fputs(",\"path\":", stdout); string(source ? menu->help : "");
        fputs(",\"depends\":", stdout); bytecode(menu->dep, "y");
        fputs(",\"visibility\":", stdout); bytecode(menu->visibility, "y");
        fputs(",\"properties\":[", stdout);
        int prop_first = 1;
        for (struct property *p = sym ? sym->prop : NULL; p; p = p->next) {
            if (p->menu != menu) continue;
            if (!prop_first) putchar(',');
            prop_first = 0;
            fputs("{\"kind\":", stdout); string(prop_get_type_name(p->type));
            fputs(",\"text\":", stdout); string(p->text);
            fputs(",\"value\":", stdout); bytecode(p->expr, "y");
            fputs(",\"when\":", stdout); bytecode(p->visible.expr, "y");
            putchar('}');
        }
        fputs("]}", stdout);
        if (menu->list) syntax_walk(menu->list, id);
    }
}

void prototype_auto_conf_string(const char *value);

int main(int argc, char **argv)
{
    if (argc == 3 && !strcmp(argv[1], "--auto-conf-string")) {
        prototype_auto_conf_string(argv[2]);
        return ferror(stdout) ? 1 : 0;
    }
    if (argc != 2) return 2;
    conf_parse(argv[1]);
    syntax_ids(rootmenu.list);
    putchar('[');
    syntax_walk(rootmenu.list, 0);
    puts("]");
    return 0;
}
