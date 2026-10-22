
#include <fcntl.h>
#include <errno.h>
#include <locale.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <wayland-client.h>
#include <wchar.h>
#include <xkbcommon/xkbcommon.h>

#include "virtual-keyboard-unstable-v1-client-protocol.h"

#define ARRAY_SIZE(x) (sizeof(x) / sizeof(x[0]))


enum wtype_command_type {
	WTYPE_COMMAND_TEXT = 0,
	WTYPE_COMMAND_MOD_PRESS = 1,
	WTYPE_COMMAND_MOD_RELEASE = 2,
	WTYPE_COMMAND_KEY_PRESS = 3,
	WTYPE_COMMAND_KEY_RELEASE = 4,
	WTYPE_COMMAND_SLEEP = 5,
	WTYPE_COMMAND_TEXT_STDIN = 6
};


enum wtype_mod {
	WTYPE_MOD_NONE = 0,
	WTYPE_MOD_SHIFT = 1,
	WTYPE_MOD_CAPSLOCK = 2,
	WTYPE_MOD_CTRL = 4,
	WTYPE_MOD_ALT = 8,
	WTYPE_MOD_MOD2 = 16,
	WTYPE_MOD_MOD3 = 32,
	WTYPE_MOD_LOGO = 64,
	WTYPE_MOD_ALTGR = 128
};


struct wtype_command {
	enum wtype_command_type type;
	union {
		struct {
			unsigned int *key_codes;
			size_t key_codes_len;
			unsigned int delay_ms;
		};
		unsigned int single_key_code;
		enum wtype_mod mod;
		unsigned int sleep_ms;
	};
};


struct keymap_entry {
	xkb_keysym_t xkb;
	wchar_t wchr;
};

// Linux evdev codes for the printable section of a PC keyboard. Chromium may
// fall back to a key's physical meaning even when its keysym is printable.
// Never put literal text on Escape, Backspace, Tab, Enter, or modifier codes.
static const unsigned int text_keycodes[] = {
	2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13,
	16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27,
	30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41,
	43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 57,
};


struct wtype {
	// Optional host-owned handshake pipes. One permission byte releases exactly
	// one complete text key pair; acknowledgement follows key release.
	int step_fd;
	int step_ack_fd;
	struct wl_display *display;
	struct wl_registry *registry;
	struct wl_seat *seat;
	struct zwp_virtual_keyboard_manager_v1 *manager;
	struct zwp_virtual_keyboard_v1 *keyboard;

	// Stores a one-indexed logical code -> (xkb_keysym_t, wchar_t) mapping.
	// Explicit key commands use these codes directly. Literal-only invocations
	// translate them to printable physical codes when uploading/typing a batch.
	// Beware that the wchar_t does not have to be valid (and is 0 in such cases)
	// This is since some keysyms need not have a unicode representation
	// (such as the arrow keys and similar)
	size_t keymap_len;
	struct keymap_entry *keymap;
	bool literal_only;
	size_t keymap_offset;

	uint32_t mod_status;
	size_t command_count;
	struct wtype_command *commands;
};


void fail(const char *format, ...)
{
	va_list vas;
	va_start(vas, format);
	vfprintf(stderr, format, vas);
	va_end(vas);
	fprintf(stderr, "\n");
	exit(EXIT_FAILURE);
}

static void upload_keymap(struct wtype *wtype);


static void handle_wl_event(void *data, struct wl_registry *registry,
							uint32_t name, const char *interface,
							uint32_t version)
{
	struct wtype *wtype = data;
	if (!strcmp(interface, wl_seat_interface.name)) {
		wtype->seat = wl_registry_bind(
			registry, name, &wl_seat_interface, version <= 7 ? version : 7
		);
	} else if (!strcmp(interface, zwp_virtual_keyboard_manager_v1_interface.name)) {
		wtype->manager = wl_registry_bind(
			registry, name, &zwp_virtual_keyboard_manager_v1_interface, 1
		);
	}
}

static void handle_wl_event_remove(void *data, struct wl_registry *registry,
								   uint32_t name)
{
}


static const struct wl_registry_listener registry_listener = {
	.global = handle_wl_event,
	.global_remove = handle_wl_event_remove,
};


static const struct { const char *name; enum wtype_mod mod; } mod_names[] = {
	{"shift", WTYPE_MOD_SHIFT},
	{"capslock", WTYPE_MOD_CAPSLOCK},
	{"ctrl", WTYPE_MOD_CTRL},
	{"logo", WTYPE_MOD_LOGO},
	{"win", WTYPE_MOD_LOGO},
	{"alt", WTYPE_MOD_ALT},
	{"altgr", WTYPE_MOD_ALTGR},
	{"mod1", WTYPE_MOD_ALT},
	{"mod2", WTYPE_MOD_MOD2},
	{"mod3", WTYPE_MOD_MOD3},
	{"mod4", WTYPE_MOD_ALTGR},
};


enum wtype_mod name_to_mod(const char *name)
{
	for (unsigned int i = 0; i < ARRAY_SIZE(mod_names); i++) {
		if (!strcasecmp(mod_names[i].name, name)) {
			return mod_names[i].mod;
		}
	}
	return WTYPE_MOD_NONE;
}


static unsigned int append_keymap_entry(struct wtype *wtype, wchar_t ch, xkb_keysym_t xkb)
{
	wtype->keymap = realloc(
		wtype->keymap, ++wtype->keymap_len * sizeof(wtype->keymap[0])
	);
	wtype->keymap[wtype->keymap_len - 1].wchr = ch;
	wtype->keymap[wtype->keymap_len - 1].xkb = xkb;
	return wtype->keymap_len;
}


unsigned int get_key_code_by_wchar(struct wtype *wtype, wchar_t ch)
{
	const struct {
		wchar_t from;
		xkb_keysym_t to;
	} remap_table[] = {
		{ L'\n', XKB_KEY_Return },
		{ L'\t', XKB_KEY_Tab },
		{ L'\e', XKB_KEY_Escape },
	};
	for (unsigned int i = 0; i < wtype->keymap_len; i++) {
		if (wtype->keymap[i].wchr == ch) {
			return i + 1;
		}
	}

	// TODO: Maybe warn if this actually ends up being XKB_KEY_NoSymbol or something?
	xkb_keysym_t xkb = xkb_utf32_to_keysym(ch);
	for (size_t i = 0; i < ARRAY_SIZE(remap_table); i++) {
		if (remap_table[i].from == ch) {
			// This overwrites whatever xkb gave us before.
			xkb = remap_table[i].to;
			break;
		}
	}

	return append_keymap_entry(wtype, ch, xkb);
}

unsigned int get_key_code_by_xkb(struct wtype *wtype, xkb_keysym_t xkb)
{
	for (unsigned int i = 0; i < wtype->keymap_len; i++) {
		if (wtype->keymap[i].xkb == xkb) {
			return i + 1;
		}
	}

	return append_keymap_entry(wtype, 0, xkb);
}


static void parse_args(struct wtype *wtype, int argc, const char *argv[])
{
	wtype->commands = calloc(argc, sizeof(wtype->commands[0])); // Upper bound
	wtype->command_count = 0;
	bool raw_text = false;
	bool prefix_with_space = false;
	bool use_stdin = false;
	unsigned int delay_ms = 0;
	wtype->literal_only = true;
	for (int i = 1; i < argc; i++) {
		struct wtype_command *cmd = &wtype->commands[wtype->command_count];
		if (!raw_text && !strcmp("--", argv[i])) {
			raw_text = true;
		} else if (!raw_text && !strcmp("-", argv[i])) {
			// Output text from stdin
			if (use_stdin) {
				fail("Stdin place-holder can only appear once");
			}
			use_stdin = true;
			cmd->type = WTYPE_COMMAND_TEXT_STDIN;
			cmd->delay_ms = delay_ms;
			wtype->command_count++;
		} else if (!raw_text && argv[i][0] == '-'){
			if (i == argc - 1) {
				fail("Missing argument to %s", argv[i]);
			}
			if (!strcmp("-M", argv[i])) {
				wtype->literal_only = false;
				// Press modifier
				cmd->type = WTYPE_COMMAND_MOD_PRESS;
				cmd->mod = name_to_mod(argv[i + 1]);
				if (cmd->mod == WTYPE_MOD_NONE) {
					fail("Invalid modifier name '%s'", argv[i + 1]);
				}
			} else if (!strcmp("-m", argv[i])) {
				wtype->literal_only = false;
				// Release modifier
				cmd->type = WTYPE_COMMAND_MOD_RELEASE;
				cmd->mod = name_to_mod(argv[i + 1]);
				if (cmd->mod == WTYPE_MOD_NONE) {
					fail("Invalid modifier name '%s'", argv[i + 1]);
				}
			} else if (!strcmp("-s", argv[i])) {
				// Sleep
				cmd->type = WTYPE_COMMAND_SLEEP;
				cmd->sleep_ms = atoi(argv[i + 1]);
				if (cmd->sleep_ms <= 0) {
					fail("Invalid sleep time");
				}
			} else if (!strcmp("-d", argv[i])) {
				// Set delay between type keystrokes
				delay_ms = atoi(argv[i + 1]);
				if (delay_ms <= 0) {
					fail("Invalid sleep time");
				}
			} else if (!strcmp("-k", argv[i])) {
				wtype->literal_only = false;
				//size_t k;
				xkb_keysym_t ks = xkb_keysym_from_name(argv[i + 1], XKB_KEYSYM_CASE_INSENSITIVE);
				if (ks == XKB_KEY_NoSymbol) {
					fail("Unknown key '%s'", argv[i + 1]);
				}
				cmd->type = WTYPE_COMMAND_TEXT;
				cmd->key_codes = malloc(sizeof(cmd->key_codes[0]));
				cmd->key_codes_len = 1;
				cmd->key_codes[0] = get_key_code_by_xkb(wtype, ks);
				cmd->delay_ms = delay_ms;
			} else if (!strcmp("-P", argv[i]) || !strcmp("-p", argv[i])) {
				wtype->literal_only = false;
				// Press/release a key
				xkb_keysym_t ks = xkb_keysym_from_name(argv[i + 1], XKB_KEYSYM_CASE_INSENSITIVE);
				if (ks == XKB_KEY_NoSymbol) {
					fail("Unknown key '%s'", argv[i + 1]);
				}
				cmd->type = argv[i][1] == 'P' ? WTYPE_COMMAND_KEY_PRESS : WTYPE_COMMAND_KEY_RELEASE;
				cmd->single_key_code = get_key_code_by_xkb(wtype, ks);
			} else {
				fail("Unknown parameter %s", argv[i]);
			}
			prefix_with_space = false;
			i++;
			wtype->command_count++;
		} else {
			// Text
			cmd->type = WTYPE_COMMAND_TEXT;
			cmd->delay_ms = delay_ms;
			wtype->command_count++;

			size_t raw_len = strlen(argv[i]) + 2; // NULL byte and the potential space
			wchar_t text[raw_len];  // Upper bound on size
			memset(text, 0, sizeof(text));
			if (!setlocale(LC_CTYPE, "")) { fail("Requested text locale is unavailable"); }
			ssize_t ret = mbstowcs(
				prefix_with_space ? &text[1] : text,
				argv[i],
				prefix_with_space ? ARRAY_SIZE(text) - 1 : ARRAY_SIZE(text)
			);
			if (ret < 0) {
				fail("Failed to deencode input argv");
			}
			if (prefix_with_space) {
				text[0] = L' ';
				ret++;
			}
			cmd->key_codes = calloc(ret, sizeof(cmd->key_codes[0]));
			cmd->key_codes_len = ret;
			for (ssize_t k = 0; k < ret; k++) {
				cmd->key_codes[k] = get_key_code_by_wchar(wtype, text[k]);
			}
			prefix_with_space = true;
		}
	}
}


static void run_sleep(struct wtype *wtype, struct wtype_command *cmd)
{
	usleep(cmd->sleep_ms * 1000);
}


static void run_mod(struct wtype *wtype, struct wtype_command *cmd)
{
	if (cmd->type == WTYPE_COMMAND_MOD_PRESS) {
		wtype->mod_status |= cmd->mod;
	} else {
		wtype->mod_status &= ~cmd->mod;
	}

	zwp_virtual_keyboard_v1_modifiers(
		wtype->keyboard, wtype->mod_status & ~WTYPE_MOD_CAPSLOCK, 0,
		wtype->mod_status & WTYPE_MOD_CAPSLOCK, 0
	);

	wl_display_roundtrip(wtype->display);
}


static void run_key(struct wtype *wtype, struct wtype_command *cmd)
{
	zwp_virtual_keyboard_v1_key(
		wtype->keyboard, 0, cmd->single_key_code,
		cmd->type == WTYPE_COMMAND_KEY_PRESS ?
			WL_KEYBOARD_KEY_STATE_PRESSED : WL_KEYBOARD_KEY_STATE_RELEASED
	);
	wl_display_roundtrip(wtype->display);
}

static void host_wait_for_permission(struct wtype *wtype)
{
	if (wtype->step_fd >= 0) {
		char ready = 'R';
		ssize_t n;
		do { n = write(wtype->step_ack_fd, &ready, 1); } while (n < 0 && errno == EINTR);
		if (n != 1) { fail("Host readiness pipe failed"); }
		char permission;
		do { n = read(wtype->step_fd, &permission, 1); } while (n < 0 && errno == EINTR);
		if (n != 1 || permission != 'T') {
			fail("Host stopped text input");
		}
	}
}

static void type_keycode(struct wtype *wtype, unsigned int key_code)
{
	if (wtype->literal_only) {
		// Reuse printable physical codes in bounded keymaps. A map changes only
		// between complete press/release pairs, never while a key is held.
		size_t offset = ((key_code - 1) / ARRAY_SIZE(text_keycodes))
			* ARRAY_SIZE(text_keycodes);
		if (offset != wtype->keymap_offset) {
			wtype->keymap_offset = offset;
			upload_keymap(wtype);
		}
		key_code = text_keycodes[(key_code - 1) % ARRAY_SIZE(text_keycodes)];
	}
	// Every required map change is complete before R. The host checks current
	// authority/target after R and sends T only when input can start now.
	host_wait_for_permission(wtype);

	zwp_virtual_keyboard_v1_key(
			wtype->keyboard, 0, key_code, WL_KEYBOARD_KEY_STATE_PRESSED
	);
	if (wl_display_roundtrip(wtype->display) < 0) {
		// Attempt release even when the connection may already be broken.
		zwp_virtual_keyboard_v1_key(wtype->keyboard, 0, key_code, WL_KEYBOARD_KEY_STATE_RELEASED);
		wl_display_flush(wtype->display);
		fail("Wayland key press was not acknowledged");
	}
	usleep(2000);
	zwp_virtual_keyboard_v1_key(
		wtype->keyboard, 0, key_code, WL_KEYBOARD_KEY_STATE_RELEASED
	);
	if (wl_display_roundtrip(wtype->display) < 0) {
		fail("Wayland key release was not acknowledged");
	}
	usleep(2000);
	if (wtype->step_ack_fd >= 0) {
		char acknowledgement = 'D';
		ssize_t n;
		do { n = write(wtype->step_ack_fd, &acknowledgement, 1); } while (n < 0 && errno == EINTR);
		if (n != 1) { fail("Host acknowledgement pipe failed"); }
	}
}

static void run_text(struct wtype *wtype, struct wtype_command *cmd)
{
	for (size_t i = 0; i < cmd->key_codes_len; i++) {
		type_keycode(wtype, cmd->key_codes[i]);
		usleep(cmd->delay_ms * 1000);
	}
}

static void run_text_stdin(struct wtype *wtype, struct wtype_command *cmd)
{
	char kbuf[8];
	size_t k = 0;
	wchar_t text_char;
	const size_t buf_size = 100;

	cmd->key_codes = calloc(buf_size, sizeof(cmd->key_codes[0]));
	cmd->key_codes_len = 0;

	if (!setlocale(LC_CTYPE, "")) { fail("Requested text locale is unavailable"); }
	while (1) {
		if ((kbuf[k] = fgetc(stdin)) == EOF) {
			break;
		}

		kbuf[k+1] = '\0';
		size_t ret = mbstowcs(&text_char, kbuf, 1);
		if (ret == (size_t)-1) {
			k++;
			if (k >= ARRAY_SIZE(kbuf) - 1) {
				break;
			}
			continue;
		} else if (ret == 0) {
			k = 0;
			continue;
		}
		k = 0;

		unsigned int key_code = get_key_code_by_wchar(wtype, text_char);
		cmd->key_codes[cmd->key_codes_len++] = key_code;

		if (cmd->key_codes_len == buf_size) {
			upload_keymap(wtype);
			for (size_t i = 0; i < cmd->key_codes_len; i++) {
				type_keycode(wtype, cmd->key_codes[i]);
				usleep(cmd->delay_ms * 1000);
			}
			cmd->key_codes_len = 0;
		}
	}

	if (cmd->key_codes_len != 0) {
		upload_keymap(wtype);
		for (size_t i = 0; i < cmd->key_codes_len; i++) {
			type_keycode(wtype, cmd->key_codes[i]);
			usleep(cmd->delay_ms * 1000);
		}
	}

	free(cmd->key_codes);
}

static void run_commands(struct wtype *wtype)
{
	void (*handlers[])(struct wtype *, struct wtype_command *) = {
		[WTYPE_COMMAND_SLEEP] = run_sleep,
		[WTYPE_COMMAND_MOD_PRESS] = run_mod,
		[WTYPE_COMMAND_MOD_RELEASE] = run_mod,
		[WTYPE_COMMAND_KEY_PRESS] = run_key,
		[WTYPE_COMMAND_KEY_RELEASE] = run_key,
		[WTYPE_COMMAND_TEXT] = run_text,
		[WTYPE_COMMAND_TEXT_STDIN] = run_text_stdin,
	};
	for (unsigned int i = 0; i < wtype->command_count; i++) {
		handlers[wtype->commands[i].type](wtype, &wtype->commands[i]);
	}
}


static void print_keysym_name(xkb_keysym_t keysym, FILE *f)
{
	char sym_name[256];

	int ret = xkb_keysym_get_name(keysym, sym_name, sizeof(sym_name));
	if (ret <= 0) {
		fail("Unable to get XKB symbol name for keysym %04x\n", keysym);
		return;
	}

	fprintf(f, "%s", sym_name);
}


static void upload_keymap(struct wtype *wtype)
{
	size_t first = wtype->literal_only ? wtype->keymap_offset : 0;
	size_t end = wtype->keymap_len;
	if (wtype->literal_only && end > first + ARRAY_SIZE(text_keycodes)) {
		end = first + ARRAY_SIZE(text_keycodes);
	}
	// Explicit key/modifier commands keep their original mapping and lifetime.
	size_t maximum = wtype->literal_only
		? text_keycodes[ARRAY_SIZE(text_keycodes) - 1] + 8
		: wtype->keymap_len + 9;

	char filename[] = "/tmp/wtype-XXXXXX";
	int fd = mkstemp(filename);
	if (fd < 0) {
		fail("Failed to create the temporary keymap file");
	}
	unlink(filename);
	FILE *f = fdopen(fd, "w");

	fprintf(f, "xkb_keymap {\n");

	fprintf(
		f,
		"xkb_keycodes \"(unnamed)\" {\n"
		"minimum = 8;\n"
		"maximum = %ld;\n",
		maximum
	);
	for (size_t i = first; i < end; i++) {
		size_t code = wtype->literal_only ? text_keycodes[i - first] : i + 1;
		fprintf(f, "<K%ld> = %ld;\n", i + 1, code + 8);
	}
	fprintf(f, "};\n");

	// TODO: Is including "complete" here really a good idea?
	fprintf(f, "xkb_types \"(unnamed)\" { include \"complete\" };\n");
	fprintf(f, "xkb_compatibility \"(unnamed)\" { include \"complete\" };\n");

	fprintf(f, "xkb_symbols \"(unnamed)\" {\n");
	for (size_t i = first; i < end; i++) {
		fprintf(f, "key <K%ld> {[", i + 1);
		print_keysym_name(wtype->keymap[i].xkb, f);
		fprintf(f, "]};\n");
	}
	fprintf(f, "};\n");

	fprintf(f, "};\n");
	fputc('\0', f);
	fflush(f);
	size_t keymap_size = ftell(f);

	zwp_virtual_keyboard_v1_keymap(
		wtype->keyboard, WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1, fileno(f), keymap_size
	);

	if (wl_display_roundtrip(wtype->display) < 0) {
		fail("Wayland keymap was not acknowledged");
	}

	fclose(f);
}

struct host_key {
	const char *name;
	unsigned int code;
	const char *modifier;
};

// Fixed evdev codes, independent of the seat's current (possibly IME-owned)
// keymap. This mode always installs its own complete private keyboard map.
static const struct host_key host_keys[] = {
	{"Tab", 15, NULL}, {"Shift+Tab", 15, XKB_MOD_NAME_SHIFT},
	{"Escape", 1, NULL}, {"Left", 105, NULL}, {"Right", 106, NULL},
	{"Up", 103, NULL}, {"Down", 108, NULL}, {"Home", 102, NULL},
	{"End", 107, NULL}, {"PageUp", 104, NULL}, {"PageDown", 109, NULL},
	{"Ctrl+A", 30, XKB_MOD_NAME_CTRL}, {"Backspace", 14, NULL}, {"Delete", 111, NULL},
};

static const struct host_key *find_host_key(const char *name)
{
	for (size_t i = 0; i < ARRAY_SIZE(host_keys); i++) {
		if (!strcmp(name, host_keys[i].name)) { return &host_keys[i]; }
	}
	fail("Unsupported host navigation key");
	return NULL;
}

static void host_key_failure(struct wtype *wtype, unsigned int code, const char *message)
{
	// The connection may already be broken. Always attempt key AND modifier
	// release before exiting; no acknowledgement is emitted on this path.
	zwp_virtual_keyboard_v1_key(wtype->keyboard, 0, code, WL_KEYBOARD_KEY_STATE_RELEASED);
	zwp_virtual_keyboard_v1_modifiers(wtype->keyboard, 0, 0, 0, 0);
	wl_display_flush(wtype->display);
	fail("%s", message);
}

static void run_host_key(struct wtype *wtype, const struct host_key *key)
{
	struct xkb_context *context = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
	if (!context) { fail("Host navigation XKB context failed"); }
	struct xkb_rule_names names = {.rules = "evdev", .model = "pc105", .layout = "us"};
	struct xkb_keymap *map = xkb_keymap_new_from_names(context, &names, XKB_KEYMAP_COMPILE_NO_FLAGS);
	if (!map) { fail("Host navigation keymap failed"); }
	xkb_mod_mask_t mods = 0;
	if (key->modifier) {
		xkb_mod_index_t index = xkb_keymap_mod_get_index(map, key->modifier);
		if (index == XKB_MOD_INVALID || index >= 32) { fail("Host navigation modifier unavailable"); }
		mods = (xkb_mod_mask_t)1u << index;
	}
	char *serialized = xkb_keymap_get_as_string(map, XKB_KEYMAP_FORMAT_TEXT_V1);
	if (!serialized) { fail("Host navigation keymap serialization failed"); }
	char filename[] = "/tmp/linux-cua-keymap-XXXXXX";
	int fd = mkstemp(filename);
	if (fd < 0) { fail("Host navigation temporary keymap failed"); }
	unlink(filename);
	FILE *stream = fdopen(fd, "w");
	if (!stream) { close(fd); fail("Host navigation keymap stream failed"); }
	size_t size = strlen(serialized) + 1;
	if (fwrite(serialized, 1, size, stream) != size || fflush(stream)) {
		fail("Host navigation keymap write failed");
	}
	zwp_virtual_keyboard_v1_keymap(wtype->keyboard, WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1,
		fileno(stream), size);
	if (wl_display_roundtrip(wtype->display) < 0) { fail("Host navigation keymap was not acknowledged"); }
	fclose(stream);
	free(serialized);
	xkb_keymap_unref(map);
	xkb_context_unref(context);

	host_wait_for_permission(wtype);
	zwp_virtual_keyboard_v1_modifiers(wtype->keyboard, mods, 0, 0, 0);
	if (wl_display_roundtrip(wtype->display) < 0) {
		host_key_failure(wtype, key->code, "Host navigation modifiers were not acknowledged");
	}
	zwp_virtual_keyboard_v1_key(wtype->keyboard, 0, key->code, WL_KEYBOARD_KEY_STATE_PRESSED);
	if (wl_display_roundtrip(wtype->display) < 0) {
		host_key_failure(wtype, key->code, "Host navigation press was not acknowledged");
	}
	usleep(2000);
	zwp_virtual_keyboard_v1_key(wtype->keyboard, 0, key->code, WL_KEYBOARD_KEY_STATE_RELEASED);
	if (wl_display_roundtrip(wtype->display) < 0) {
		host_key_failure(wtype, key->code, "Host navigation release was not acknowledged");
	}
	zwp_virtual_keyboard_v1_modifiers(wtype->keyboard, 0, 0, 0, 0);
	if (wl_display_roundtrip(wtype->display) < 0) {
		host_key_failure(wtype, key->code, "Host navigation modifier release was not acknowledged");
	}
	char acknowledgement = 'D';
	ssize_t n;
	do { n = write(wtype->step_ack_fd, &acknowledgement, 1); } while (n < 0 && errno == EINTR);
	if (n != 1) { fail("Host navigation acknowledgement pipe failed"); }
}


int main(int argc, const char *argv[])
{
	if (argc < 2) {
		fail("Usage: %s <text-to-type>", argv[0]);
	}

	struct wtype wtype;
	memset(&wtype, 0, sizeof(wtype));
	wtype.step_fd = -1;
	wtype.step_ack_fd = -1;
	const char *step = getenv("LINUX_CUA_STEP_FD");
	const char *ack = getenv("LINUX_CUA_STEP_ACK_FD");
	if (step || ack) {
		if (!step || !ack) { fail("Both host handshake descriptors are required"); }
		char *end_step, *end_ack;
		long input_fd = strtol(step, &end_step, 10);
		long output_fd = strtol(ack, &end_ack, 10);
		if (*end_step || *end_ack || input_fd < 3 || output_fd < 3 ||
			input_fd > 1024 || output_fd > 1024 || input_fd == output_fd) {
			fail("Invalid host handshake descriptors");
		}
		wtype.step_fd = input_fd;
		wtype.step_ack_fd = output_fd;
	}

	const struct host_key *host_key = NULL;
	if (!strcmp(argv[1], "--host-key")) {
		if (argc != 3) { fail("Host navigation requires one allowlisted key"); }
		host_key = find_host_key(argv[2]);
		if (wtype.step_fd < 0) { fail("Host navigation requires handshake descriptors"); }
	} else {
		parse_args(&wtype, argc, argv);
	}
	if (wtype.step_fd >= 0 && !wtype.literal_only && !host_key) {
		fail("Host handshake supports literal text only");
	}

	wtype.display = wl_display_connect(NULL);
	if (wtype.display == NULL) {
		fail("Wayland connection failed");
	}
	wtype.registry = wl_display_get_registry(wtype.display);
	wl_registry_add_listener(wtype.registry, &registry_listener, &wtype);
	if (wl_display_dispatch(wtype.display) < 0 || wl_display_roundtrip(wtype.display) < 0) {
		fail("Wayland initialization failed");
	}

	if (wtype.manager == NULL) {
		fail("Compositor does not support the virtual keyboard protocol");
	}
	if (wtype.seat == NULL) {
		fail("No seat found");
	}

	wtype.keyboard = zwp_virtual_keyboard_manager_v1_create_virtual_keyboard(
		wtype.manager, wtype.seat
	);

	if (host_key) {
		run_host_key(&wtype, host_key);
	} else {
		upload_keymap(&wtype);
		run_commands(&wtype);
	}

	for (unsigned int i = 0; i < wtype.command_count; i++) {
		if (wtype.commands[i].type == WTYPE_COMMAND_TEXT) {
			free(wtype.commands[i].key_codes);
		}
	}
	free(wtype.commands);
	free(wtype.keymap);
	zwp_virtual_keyboard_v1_destroy(wtype.keyboard);
	// Do not report successful process completion until the compositor has
	// processed removal and can restore its previous active keyboard/keymap.
	if (wl_display_roundtrip(wtype.display) < 0) {
		fail("Wayland virtual keyboard removal was not acknowledged");
	}
	zwp_virtual_keyboard_manager_v1_destroy(wtype.manager);
	wl_registry_destroy(wtype.registry);
	wl_display_disconnect(wtype.display);
}
