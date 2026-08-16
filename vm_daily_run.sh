#!/bin/bash
# Daily trade pipeline — download + import run on VM (Indian IP can reach CBOS).
# GHA runners cannot reach backoffice.motilaloswal.com or be SSH'd into from outside.
# So: VM downloads CSVs, imports to trades.json, pushes to git, then triggers
# GHA with skip_download=true for GSheet sync + Telegram notification.
#
# Cron:   Mon-Sat 14:00 UTC (7:30 PM IST)  — incremental
#         Sun     05:30 UTC (11:00 AM IST)  — full

set -e
export PATH=/usr/local/bin:/usr/bin:/bin:/home/opc/.local/bin:/sbin:/usr/sbin:$PATH

IS_FULL=${1:-false}
REPO_DIR=/home/opc/app
REPO="jainrishank20/client-tracker-mofsl"
LOG=/home/opc/vm_daily_run.log
LOG_START=0  # line number where current run starts — set inside the block below

_tg_notify() {
  TG_TOKEN=$(python3 -c "import json; c=json.load(open('${REPO_DIR}/bot_config.json',encoding='utf-8-sig')); print(c.get('telegram_token',''))" 2>/dev/null)
  TG_CHAT=$(python3 -c "import json; c=json.load(open('${REPO_DIR}/bot_config.json',encoding='utf-8-sig')); print(str(c.get('allowed_chat_id','')).split(',')[0].strip())" 2>/dev/null)
  if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
    # Read only THIS run's log output (not stale entries from previous runs)
    THIS_RUN=$(tail -n "+${LOG_START}" "$LOG" 2>/dev/null || tail -20 "$LOG" 2>/dev/null)
    LAST=$(echo "$THIS_RUN" | grep "=== Done" | wc -l)
    if [ "$LAST" -gt 0 ]; then
      MSG="VM download done ($(TZ='Asia/Kolkata' date '+%d %b %I:%M %p IST')) — GHA running GSheet sync. Check /vmlog."
    else
      ERRMSG=$(echo "$THIS_RUN" | grep -i "error\|fail\|traceback\|exception" | grep -v "continue.on.error\|non.fatal\|skipping" | tail -1)
      MSG="Pipeline FAILED at $(TZ='Asia/Kolkata' date '+%d %b %I:%M %p IST'). ${ERRMSG:-Check /vmlog.}"
    fi
    curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
      -d "chat_id=${TG_CHAT}&text=${MSG}" > /dev/null
  fi
}
trap _tg_notify EXIT

{
LOG_START=$(wc -l < "$LOG" 2>/dev/null || echo 0)
LOG_START=$((LOG_START + 1))
echo ""
echo "=== $(date '+%Y-%m-%d %H:%M:%S') — IS_FULL=$IS_FULL ==="
cd "$REPO_DIR"

# Load GitHub token from bot_config.json
GITHUB_TOKEN=$(python3 -c "
import json, sys
try:
    c = json.load(open('bot_config.json', encoding='utf-8-sig'))
    t = c.get('github_token', '')
    if not t: raise ValueError('github_token is empty')
    print(t)
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
")

# ── Ensure playwright + chromium are installed ───────────────────────────────
if ! python3 -c "import playwright" 2>/dev/null; then
  echo "Installing playwright..."
  python3 -m pip install playwright --quiet || true
  python3 -m playwright install chromium || true
fi

# ── Ensure chromium + system libs are set up for Oracle Linux 9 ──────────────
# OL9 minimal install is missing many libs chromium needs.
# Fix: compile GCC stubs with proper symbols for all missing libs.
# Stubs return safe no-op values — chromium checks returns before use.

LIBDIR=/home/opc/lib
SETUP_MARKER=/home/opc/.chromium_setup_v2
mkdir -p "$LIBDIR"

if [ ! -f "$SETUP_MARKER" ]; then
  echo "Setting up chromium for Oracle Linux 9..."

  # Clean any old stubs from previous setup versions
  rm -f "$LIBDIR"/*.so* 2>/dev/null || true
  echo "  Cleared old stubs."

  # Reinstall playwright + chromium (in case binary is missing or stale)
  python3 -m pip install playwright --quiet 2>/dev/null || true
  python3 -m playwright install chromium 2>/dev/null || true
  echo "  Playwright + chromium reinstalled."

  # Compile GCC stubs for all libs missing on OL9 minimal.
  # These have proper function implementations (correct return types, no-op bodies).
  # Chromium uses these libs optionally (ATK, audio, X11 extensions) and handles NULL returns.
  echo "  Compiling GCC stubs for missing libs..."

  # --- ATK / ATSPI stubs (libatk-1.0.so.0, libatk-bridge-2.0.so.0, libatspi.so.0) ---
  cat > /tmp/atk_stub.c << 'CEOF'
#include <stddef.h>
#include <stdint.h>
typedef void* gpointer;
typedef unsigned long GType;

int atk_action_get_n_actions(gpointer a){return 0;}
const char* atk_action_get_name(gpointer a,int i){return NULL;}
GType atk_action_get_type(void){return 0;}
int atk_add_global_event_listener(gpointer f,const char* t){return 0;}
void atk_attribute_set_free(gpointer s){}
void atk_bridge_adaptor_init(void){}
void atk_bridge_adaptor_cleanup(void){}
GType atk_component_get_type(void){return 0;}
GType atk_document_get_type(void){return 0;}
GType atk_editable_text_get_type(void){return 0;}
const char* atk_get_version(void){return "0";}
gpointer atk_get_root(void){return NULL;}
gpointer atk_get_focus_object(void){return NULL;}
GType atk_hyperlink_get_type(void){return 0;}
int atk_hyperlink_get_start_index(gpointer h){return 0;}
GType atk_hyperlink_impl_get_type(void){return 0;}
gpointer atk_hypertext_get_link(gpointer h,int i){return NULL;}
int atk_hypertext_get_n_links(gpointer h){return 0;}
GType atk_hypertext_get_type(void){return 0;}
GType atk_image_get_type(void){return 0;}
GType atk_implementor_get_type(void){return 0;}
gpointer atk_implementor_ref_accessible(gpointer i){return NULL;}
gpointer atk_no_op_object_new(gpointer obj){return NULL;}
void atk_object_get_attributes(void){}
const char* atk_object_get_description(gpointer o){return NULL;}
int atk_object_get_index_in_parent(gpointer o){return -1;}
const char* atk_object_get_name(gpointer o){return NULL;}
int atk_object_get_n_accessible_children(gpointer o){return 0;}
gpointer atk_object_get_parent(gpointer o){return NULL;}
int atk_object_get_role(gpointer o){return 0;}
GType atk_object_get_type(void){return 0;}
void atk_object_initialize(gpointer o,gpointer d){}
void atk_object_notify_state_change(gpointer o,unsigned long s,int v){}
gpointer atk_object_ref_accessible_child(gpointer o,int i){return NULL;}
gpointer atk_object_ref_relation_set(gpointer o){return NULL;}
gpointer atk_object_ref_state_set(gpointer o){return NULL;}
void atk_object_set_name(gpointer o,const char* n){}
void atk_object_set_role(gpointer o,int r){}
void atk_object_set_parent(gpointer o,gpointer p){}
gpointer atk_relation_get_target(gpointer r){return NULL;}
void atk_relation_set_add_relation_by_type(gpointer s,int t,gpointer o){}
int atk_relation_set_contains(gpointer s,int t){return 0;}
gpointer atk_relation_set_get_relation_by_type(gpointer s,int t){return NULL;}
gpointer atk_relation_set_new(void){return NULL;}
const char* atk_relation_type_get_name(int t){return NULL;}
GType atk_relation_type_get_type(void){return 0;}
void atk_remove_global_event_listener(int i){}
GType atk_selection_get_type(void){return 0;}
void atk_state_set_add_state(gpointer s,unsigned long t){}
int atk_state_set_contains_state(gpointer s,unsigned long t){return 0;}
gpointer atk_state_set_new(void){return NULL;}
const char* atk_state_type_get_name(unsigned long t){return NULL;}
GType atk_state_type_get_type(void){return 0;}
GType atk_state_type_register(void){return 0;}
gpointer atk_table_cell_get_column_header_cells(gpointer c){return NULL;}
int atk_table_cell_get_row_column_span(gpointer c,int* r,int* col,int* rspan,int* cspan){return 0;}
gpointer atk_table_cell_get_row_header_cells(gpointer c){return NULL;}
GType atk_table_cell_get_type(void){return 0;}
gpointer atk_table_get_caption(gpointer t){return NULL;}
const char* atk_table_get_column_description(gpointer t,int c){return NULL;}
int atk_table_get_column_extent_at(gpointer t,int r,int c){return 0;}
GType atk_table_get_type(void){return 0;}
int atk_table_get_n_columns(gpointer t){return 0;}
int atk_table_get_n_rows(gpointer t){return 0;}
const char* atk_table_get_row_description(gpointer t,int r){return NULL;}
int atk_table_get_row_extent_at(gpointer t,int r,int c){return 0;}
const char* atk_text_attribute_get_name(int a){return NULL;}
int atk_text_get_caret_offset(gpointer t){return 0;}
int atk_text_get_character_count(gpointer t){return 0;}
gpointer atk_text_get_run_attributes(gpointer t,int o,int* s,int* e){return NULL;}
gpointer atk_text_get_selection(gpointer t,int i,int* s,int* e){return NULL;}
char* atk_text_get_text(gpointer t,int s,int e){return NULL;}
GType atk_text_get_type(void){return 0;}
GType atk_util_get_type(void){return 0;}
void atk_value_get_current_value(gpointer v,gpointer val){}
void atk_value_get_maximum_value(gpointer v,gpointer val){}
void atk_value_get_minimum_value(gpointer v,gpointer val){}
GType atk_value_get_type(void){return 0;}
GType atk_window_get_type(void){return 0;}
gpointer atspi_accessible_get_application(gpointer a,gpointer* e){return NULL;}
gpointer atspi_accessible_get_attributes(gpointer a,gpointer* e){return NULL;}
gpointer atspi_accessible_get_child_at_index(gpointer a,int i,gpointer* e){return NULL;}
int atspi_accessible_get_child_count(gpointer a,gpointer* e){return 0;}
char* atspi_accessible_get_description(gpointer a,gpointer* e){return NULL;}
gpointer atspi_accessible_get_interfaces(gpointer a){return NULL;}
char* atspi_accessible_get_name(gpointer a,gpointer* e){return NULL;}
gpointer atspi_accessible_get_parent(gpointer a,gpointer* e){return NULL;}
int atspi_accessible_get_process_id(gpointer a,gpointer* e){return 0;}
gpointer atspi_accessible_get_relation_set(gpointer a,gpointer* e){return NULL;}
int atspi_accessible_get_role(gpointer a,gpointer* e){return 0;}
char* atspi_accessible_get_role_name(gpointer a,gpointer* e){return NULL;}
gpointer atspi_accessible_get_state_set(gpointer a){return NULL;}
GType atspi_accessible_get_type(void){return 0;}
GType atspi_event_get_type(void){return 0;}
int atspi_event_listener_deregister(gpointer l,const char* e,gpointer* err){return 0;}
gpointer atspi_event_listener_new(gpointer cb,gpointer data,gpointer dest){return NULL;}
int atspi_event_listener_register(gpointer l,const char* e,gpointer* err){return 0;}
gpointer atspi_get_desktop(int i){return NULL;}
int atspi_get_desktop_count(void){return 0;}
int atspi_init(void){return 0;}
int atspi_exit(void){return 0;}
int atspi_relation_get_n_targets(gpointer r){return 0;}
int atspi_relation_get_relation_type(gpointer r){return 0;}
gpointer atspi_relation_get_target(gpointer r,int i){return NULL;}
gpointer atspi_state_set_get_states(gpointer s){return NULL;}
CEOF
  gcc -shared -fPIC -o "$LIBDIR/libatk-1.0.so.0" /tmp/atk_stub.c
  gcc -shared -fPIC -o "$LIBDIR/libatk-bridge-2.0.so.0" /tmp/atk_stub.c
  gcc -shared -fPIC -o "$LIBDIR/libatspi.so.0" /tmp/atk_stub.c
  echo "    libatk-1.0.so.0, libatk-bridge-2.0.so.0, libatspi.so.0"

  # --- GBM stub (libgbm.so.1) ---
  cat > /tmp/gbm_stub.c << 'CEOF'
#include <stdint.h>
#include <stddef.h>
typedef void* gpointer;
typedef union { int s32; unsigned int u32; void* ptr; float f; } gbm_bo_handle;
gpointer gbm_create_device(int fd){return NULL;}
void gbm_device_destroy(gpointer d){}
int gbm_device_get_fd(gpointer d){return -1;}
const char* gbm_device_get_backend_name(gpointer d){return "none";}
int gbm_device_is_format_supported(gpointer d,uint32_t f,uint32_t u){return 0;}
gpointer gbm_bo_create(gpointer d,uint32_t w,uint32_t h,uint32_t f,uint32_t fl){return NULL;}
gpointer gbm_bo_create_with_modifiers(gpointer d,uint32_t w,uint32_t h,uint32_t f,const uint64_t* m,uint32_t c){return NULL;}
gpointer gbm_bo_create_with_modifiers2(gpointer d,uint32_t w,uint32_t h,uint32_t f,const uint64_t* m,uint32_t c,uint32_t fl){return NULL;}
gpointer gbm_bo_import(gpointer d,uint32_t t,void* b,uint32_t u){return NULL;}
void gbm_bo_destroy(gpointer bo){}
gpointer gbm_bo_get_device(gpointer bo){return NULL;}
uint32_t gbm_bo_get_width(gpointer bo){return 0;}
uint32_t gbm_bo_get_height(gpointer bo){return 0;}
uint32_t gbm_bo_get_format(gpointer bo){return 0;}
uint32_t gbm_bo_get_stride(gpointer bo){return 0;}
uint64_t gbm_bo_get_modifier(gpointer bo){return 0;}
int gbm_bo_get_plane_count(gpointer bo){return 0;}
uint32_t gbm_bo_get_stride_for_plane(gpointer bo,int p){return 0;}
uint32_t gbm_bo_get_offset(gpointer bo,int p){return 0;}
int gbm_bo_get_fd(gpointer bo){return -1;}
int gbm_bo_get_fd_for_plane(gpointer bo,int p){return -1;}
gbm_bo_handle gbm_bo_get_handle(gpointer bo){gbm_bo_handle h={0};return h;}
gbm_bo_handle gbm_bo_get_handle_for_plane(gpointer bo,int p){gbm_bo_handle h={0};return h;}
void* gbm_bo_map(gpointer bo,uint32_t x,uint32_t y,uint32_t w,uint32_t h,uint32_t fl,uint32_t* str,void** data){return NULL;}
void gbm_bo_unmap(gpointer bo,void* data){}
void* gbm_bo_get_user_data(gpointer bo){return NULL;}
void gbm_bo_set_user_data(gpointer bo,void* data,void* cb){}
gpointer gbm_surface_create(gpointer d,uint32_t w,uint32_t h,uint32_t f,uint32_t fl){return NULL;}
gpointer gbm_surface_create_with_modifiers(gpointer d,uint32_t w,uint32_t h,uint32_t f,const uint64_t* m,uint32_t c){return NULL;}
void gbm_surface_destroy(gpointer s){}
gpointer gbm_surface_lock_front_buffer(gpointer s){return NULL;}
void gbm_surface_release_buffer(gpointer s,gpointer bo){}
int gbm_surface_has_free_buffers(gpointer s){return 0;}
CEOF
  gcc -shared -fPIC -o "$LIBDIR/libgbm.so.1" /tmp/gbm_stub.c
  echo "    libgbm.so.1"

  # --- xkbcommon stub (libxkbcommon.so.0) — needs versioned symbols ---
  cat > /tmp/xkb_stub.c << 'CEOF'
#include <stdint.h>
#include <stddef.h>
typedef void* gpointer;
gpointer xkb_context_new(int f){return NULL;}
gpointer xkb_context_ref(gpointer c){return NULL;}
void xkb_context_unref(gpointer c){}
int xkb_context_include_path_append(gpointer c,const char* p){return 0;}
void xkb_context_set_log_fn(gpointer c,void* f){}
void xkb_context_set_log_verbosity(gpointer c,int v){}
gpointer xkb_keymap_new_from_names(gpointer c,void* n,int f){return NULL;}
gpointer xkb_keymap_new_from_string(gpointer c,const char* s,int fmt,int f){return NULL;}
gpointer xkb_keymap_new_from_buffer(gpointer c,const char* b,size_t l,int fmt,int f){return NULL;}
gpointer xkb_keymap_ref(gpointer k){return NULL;}
void xkb_keymap_unref(gpointer k){}
uint32_t xkb_keymap_min_keycode(gpointer k){return 0;}
uint32_t xkb_keymap_max_keycode(gpointer k){return 0;}
int xkb_keymap_num_mods(gpointer k){return 0;}
const char* xkb_keymap_mod_get_name(gpointer k,uint32_t i){return NULL;}
uint32_t xkb_keymap_num_layouts_for_key(gpointer k,uint32_t key){return 0;}
uint32_t xkb_keymap_num_levels_for_key(gpointer k,uint32_t key,uint32_t l){return 0;}
int xkb_keymap_key_get_syms_by_level(gpointer k,uint32_t key,uint32_t l,uint32_t lv,const uint32_t** syms){return 0;}
int xkb_keymap_key_repeats(gpointer k,uint32_t key){return 0;}
gpointer xkb_state_new(gpointer k){return NULL;}
gpointer xkb_state_ref(gpointer s){return NULL;}
void xkb_state_unref(gpointer s){}
gpointer xkb_state_get_keymap(gpointer s){return NULL;}
int xkb_state_update_mask(gpointer s,uint32_t a,uint32_t b,uint32_t c,uint32_t d,uint32_t e,uint32_t f){return 0;}
int xkb_state_key_get_syms(gpointer s,uint32_t key,const uint32_t** syms){return 0;}
uint32_t xkb_state_key_get_one_sym(gpointer s,uint32_t key){return 0;}
uint32_t xkb_state_key_get_utf32(gpointer s,uint32_t key){return 0;}
int xkb_state_mod_index_is_active(gpointer s,uint32_t i,int t){return 0;}
CEOF
  cat > /tmp/xkb_stub.ver << 'CEOF'
V_0.5.0 { global: xkb_*; local: *; };
CEOF
  gcc -shared -fPIC -Wl,--version-script=/tmp/xkb_stub.ver -o "$LIBDIR/libxkbcommon.so.0" /tmp/xkb_stub.c
  echo "    libxkbcommon.so.0"

  # --- ALSA stub (libasound.so.2) — needs versioned symbols ---
  cat > /tmp/alsa_stub.c << 'CEOF'
#include <stdint.h>
#include <stddef.h>
typedef void* gpointer;
int snd_card_next(int* c){if(c)*c=-1;return 0;}
int snd_ctl_open(gpointer* h,const char* n,int m){return -1;}
int snd_ctl_close(gpointer h){return 0;}
int snd_ctl_card_info(gpointer h,gpointer i){return -1;}
size_t snd_ctl_card_info_sizeof(void){return 256;}
const char* snd_ctl_card_info_get_driver(gpointer i){return "";}
const char* snd_ctl_card_info_get_name(gpointer i){return "";}
const char* snd_ctl_card_info_get_longname(gpointer i){return "";}
int snd_ctl_hwdep_next_device(gpointer h,int* d){if(d)*d=-1;return 0;}
int snd_ctl_hwdep_info(gpointer h,gpointer i){return -1;}
size_t snd_hwdep_info_sizeof(void){return 256;}
int snd_hwdep_info_get_iface(gpointer i){return 0;}
int snd_ctl_rawmidi_next_device(gpointer h,int* d){if(d)*d=-1;return 0;}
int snd_device_name_hint(int c,const char* i,void*** hints){if(hints)*hints=NULL;return 0;}
int snd_device_name_free_hint(void** hints){return 0;}
char* snd_device_name_get_hint(const void* h,const char* id){return NULL;}
int snd_pcm_open(gpointer* h,const char* n,int s,int m){return -1;}
int snd_pcm_close(gpointer h){return 0;}
int snd_pcm_prepare(gpointer h){return 0;}
int snd_pcm_start(gpointer h){return 0;}
int snd_pcm_drop(gpointer h){return 0;}
int snd_pcm_drain(gpointer h){return 0;}
int snd_pcm_recover(gpointer h,int e,int s){return -1;}
int snd_pcm_resume(gpointer h){return 0;}
long snd_pcm_avail_update(gpointer h){return 0;}
int snd_pcm_delay(gpointer h,long* d){return 0;}
long snd_pcm_writei(gpointer h,const void* b,unsigned long s){return -1;}
long snd_pcm_readi(gpointer h,void* b,unsigned long s){return -1;}
int snd_pcm_get_params(gpointer h,unsigned long* b,unsigned long* p){return 0;}
int snd_pcm_set_params(gpointer h,int f,int a,unsigned int c,unsigned int r,int s,unsigned int l){return -1;}
int snd_pcm_state(gpointer h){return 0;}
const char* snd_pcm_name(gpointer h){return "";}
int snd_pcm_format_size(int f,size_t s){return 0;}
int snd_pcm_hw_params(gpointer h,gpointer p){return 0;}
int snd_pcm_hw_params_any(gpointer h,gpointer p){return 0;}
int snd_pcm_hw_params_malloc(gpointer* p){if(p)*p=NULL;return 0;}
void snd_pcm_hw_params_free(gpointer p){}
int snd_pcm_hw_params_set_access(gpointer h,gpointer p,int a){return 0;}
int snd_pcm_hw_params_set_format(gpointer h,gpointer p,int f){return 0;}
int snd_pcm_hw_params_set_channels(gpointer h,gpointer p,unsigned int c){return 0;}
int snd_pcm_hw_params_set_rate_near(gpointer h,gpointer p,unsigned int* r,int* d){return 0;}
int snd_pcm_hw_params_set_rate_resample(gpointer h,gpointer p,unsigned int v){return 0;}
int snd_pcm_hw_params_set_buffer_size_near(gpointer h,gpointer p,unsigned long* b){return 0;}
int snd_pcm_hw_params_set_period_size_near(gpointer h,gpointer p,unsigned long* f,int* d){return 0;}
int snd_pcm_hw_params_get_channels_min(gpointer p,unsigned int* c){if(c)*c=2;return 0;}
int snd_pcm_hw_params_can_resume(gpointer p){return 0;}
int snd_pcm_hw_params_test_format(gpointer h,gpointer p,int f){return 0;}
int snd_pcm_sw_params(gpointer h,gpointer p){return 0;}
int snd_pcm_sw_params_current(gpointer h,gpointer p){return 0;}
int snd_pcm_sw_params_malloc(gpointer* p){if(p)*p=NULL;return 0;}
void snd_pcm_sw_params_free(gpointer p){}
int snd_pcm_sw_params_set_avail_min(gpointer h,gpointer p,unsigned long f){return 0;}
int snd_pcm_sw_params_set_start_threshold(gpointer h,gpointer p,unsigned long t){return 0;}
int snd_mixer_open(gpointer* h,int m){return -1;}
int snd_mixer_close(gpointer h){return 0;}
int snd_mixer_attach(gpointer h,const char* n){return -1;}
int snd_mixer_detach(gpointer h,const char* n){return 0;}
int snd_mixer_load(gpointer h){return 0;}
void snd_mixer_free(gpointer h){}
int snd_mixer_handle_events(gpointer h){return 0;}
int snd_mixer_poll_descriptors_count(gpointer h){return 0;}
int snd_mixer_poll_descriptors(gpointer h,void* fds,unsigned int s){return 0;}
gpointer snd_mixer_first_elem(gpointer h){return NULL;}
gpointer snd_mixer_elem_next(gpointer e){return NULL;}
void snd_mixer_elem_set_callback(gpointer e,void* f){}
void snd_mixer_elem_set_callback_private(gpointer e,void* d){}
void* snd_mixer_elem_get_callback_private(gpointer e){return NULL;}
int snd_mixer_selem_register(gpointer h,void* o,gpointer* c){return 0;}
gpointer snd_mixer_find_selem(gpointer h,gpointer id){return NULL;}
const char* snd_mixer_selem_get_name(gpointer e){return "";}
int snd_mixer_selem_is_active(gpointer e){return 0;}
int snd_mixer_selem_has_playback_volume(gpointer e){return 0;}
int snd_mixer_selem_has_playback_switch(gpointer e){return 0;}
int snd_mixer_selem_has_capture_volume(gpointer e){return 0;}
int snd_mixer_selem_get_playback_volume(gpointer e,int c,long* v){return 0;}
int snd_mixer_selem_get_capture_volume(gpointer e,int c,long* v){return 0;}
int snd_mixer_selem_get_playback_volume_range(gpointer e,long* mn,long* mx){return 0;}
int snd_mixer_selem_get_capture_volume_range(gpointer e,long* mn,long* mx){return 0;}
int snd_mixer_selem_set_playback_volume_all(gpointer e,long v){return 0;}
int snd_mixer_selem_set_capture_volume_all(gpointer e,long v){return 0;}
int snd_mixer_selem_get_playback_switch(gpointer e,int c,int* v){return 0;}
int snd_mixer_selem_set_playback_switch(gpointer e,int c,int v){return 0;}
int snd_mixer_selem_set_playback_switch_all(gpointer e,int v){return 0;}
int snd_mixer_selem_ask_playback_vol_dB(gpointer e,long v,long* d){return 0;}
int snd_mixer_selem_ask_playback_dB_vol(gpointer e,long d,int r,long* v){return 0;}
int snd_mixer_selem_id_malloc(gpointer* p){if(p)*p=NULL;return 0;}
void snd_mixer_selem_id_free(gpointer p){}
void snd_mixer_selem_id_set_index(gpointer p,unsigned int i){}
void snd_mixer_selem_id_set_name(gpointer p,const char* n){}
int snd_seq_open(gpointer* h,const char* n,int s,int m){return -1;}
int snd_seq_close(gpointer h){return 0;}
int snd_seq_client_id(gpointer h){return -1;}
int snd_seq_set_client_name(gpointer h,const char* n){return 0;}
int snd_seq_create_simple_port(gpointer h,const char* n,unsigned int c,unsigned int t){return -1;}
int snd_seq_delete_simple_port(gpointer h,int p){return 0;}
int snd_seq_event_input(gpointer h,void** e){return 0;}
int snd_seq_event_input_pending(gpointer h,int f){return 0;}
int snd_seq_event_output_direct(gpointer h,void* e){return 0;}
int snd_seq_poll_descriptors(gpointer h,void* fds,unsigned int s,short e){return 0;}
int snd_seq_get_any_client_info(gpointer h,int c,gpointer i){return -1;}
int snd_seq_get_any_port_info(gpointer h,int c,int p,gpointer i){return -1;}
int snd_seq_query_next_client(gpointer h,gpointer i){return -1;}
int snd_seq_query_next_port(gpointer h,gpointer i){return -1;}
int snd_seq_subscribe_port(gpointer h,gpointer s){return -1;}
size_t snd_seq_client_info_sizeof(void){return 256;}
int snd_seq_client_info_get_client(gpointer i){return 0;}
const char* snd_seq_client_info_get_name(gpointer i){return "";}
int snd_seq_client_info_get_type(gpointer i){return 0;}
void snd_seq_client_info_set_client(gpointer i,int c){}
size_t snd_seq_port_info_sizeof(void){return 256;}
const void* snd_seq_port_info_get_addr(gpointer i){return NULL;}
unsigned int snd_seq_port_info_get_capability(gpointer i){return 0;}
const char* snd_seq_port_info_get_name(gpointer i){return "";}
unsigned int snd_seq_port_info_get_type(gpointer i){return 0;}
void snd_seq_port_info_set_client(gpointer i,int c){}
void snd_seq_port_info_set_port(gpointer i,int p){}
size_t snd_seq_port_subscribe_sizeof(void){return 256;}
void snd_seq_port_subscribe_set_sender(gpointer s,const void* a){}
void snd_seq_port_subscribe_set_dest(gpointer s,const void* a){}
int snd_midi_event_new(size_t s,gpointer* e){if(e)*e=NULL;return 0;}
void snd_midi_event_free(gpointer e){}
void snd_midi_event_no_status(gpointer e,int v){}
long snd_midi_event_encode_byte(gpointer e,int b,void* ev){return 0;}
long snd_midi_event_decode(gpointer e,unsigned char* b,long s,const void* ev){return 0;}
const char* snd_strerror(int e){return "error";}
CEOF
  cat > /tmp/alsa_stub.ver << 'CEOF'
ALSA_0.9 {
    global:
        snd_card_next;
        snd_ctl_open; snd_ctl_close; snd_ctl_card_info;
        snd_ctl_card_info_sizeof; snd_ctl_card_info_get_driver;
        snd_ctl_card_info_get_name; snd_ctl_card_info_get_longname;
        snd_ctl_hwdep_next_device; snd_ctl_hwdep_info;
        snd_hwdep_info_sizeof; snd_hwdep_info_get_iface;
        snd_ctl_rawmidi_next_device;
        snd_device_name_hint; snd_device_name_free_hint; snd_device_name_get_hint;
        snd_pcm_open; snd_pcm_close; snd_pcm_prepare; snd_pcm_start;
        snd_pcm_drop; snd_pcm_drain; snd_pcm_recover; snd_pcm_resume;
        snd_pcm_avail_update; snd_pcm_delay; snd_pcm_writei; snd_pcm_readi;
        snd_pcm_get_params; snd_pcm_set_params; snd_pcm_state; snd_pcm_name;
        snd_pcm_format_size; snd_pcm_hw_params; snd_pcm_hw_params_any;
        snd_pcm_hw_params_malloc; snd_pcm_hw_params_free;
        snd_pcm_hw_params_set_access; snd_pcm_hw_params_set_format;
        snd_pcm_hw_params_set_channels; snd_pcm_hw_params_set_rate_resample;
        snd_pcm_hw_params_can_resume; snd_pcm_hw_params_test_format;
        snd_pcm_sw_params; snd_pcm_sw_params_current;
        snd_pcm_sw_params_malloc; snd_pcm_sw_params_free;
        snd_pcm_sw_params_set_avail_min; snd_pcm_sw_params_set_start_threshold;
        snd_mixer_open; snd_mixer_close; snd_mixer_attach; snd_mixer_detach;
        snd_mixer_load; snd_mixer_free; snd_mixer_handle_events;
        snd_mixer_poll_descriptors_count; snd_mixer_poll_descriptors;
        snd_mixer_first_elem; snd_mixer_elem_next;
        snd_mixer_elem_set_callback; snd_mixer_elem_set_callback_private;
        snd_mixer_elem_get_callback_private; snd_mixer_selem_register;
        snd_mixer_find_selem; snd_mixer_selem_get_name; snd_mixer_selem_is_active;
        snd_mixer_selem_has_playback_volume; snd_mixer_selem_has_playback_switch;
        snd_mixer_selem_has_capture_volume; snd_mixer_selem_get_playback_volume;
        snd_mixer_selem_get_capture_volume; snd_mixer_selem_get_playback_volume_range;
        snd_mixer_selem_get_capture_volume_range; snd_mixer_selem_set_playback_volume_all;
        snd_mixer_selem_set_capture_volume_all; snd_mixer_selem_get_playback_switch;
        snd_mixer_selem_set_playback_switch; snd_mixer_selem_set_playback_switch_all;
        snd_mixer_selem_ask_playback_vol_dB; snd_mixer_selem_ask_playback_dB_vol;
        snd_mixer_selem_id_malloc; snd_mixer_selem_id_free;
        snd_mixer_selem_id_set_index; snd_mixer_selem_id_set_name;
        snd_seq_open; snd_seq_close; snd_seq_client_id; snd_seq_set_client_name;
        snd_seq_create_simple_port; snd_seq_delete_simple_port;
        snd_seq_event_input; snd_seq_event_input_pending; snd_seq_event_output_direct;
        snd_seq_poll_descriptors; snd_seq_get_any_client_info; snd_seq_get_any_port_info;
        snd_seq_query_next_client; snd_seq_query_next_port; snd_seq_subscribe_port;
        snd_seq_client_info_sizeof; snd_seq_client_info_get_client;
        snd_seq_client_info_get_name; snd_seq_client_info_get_type;
        snd_seq_client_info_set_client; snd_seq_port_info_sizeof;
        snd_seq_port_info_get_addr; snd_seq_port_info_get_capability;
        snd_seq_port_info_get_name; snd_seq_port_info_get_type;
        snd_seq_port_info_set_client; snd_seq_port_info_set_port;
        snd_seq_port_subscribe_sizeof; snd_seq_port_subscribe_set_sender;
        snd_seq_port_subscribe_set_dest;
        snd_midi_event_new; snd_midi_event_free; snd_midi_event_no_status;
        snd_midi_event_encode_byte; snd_midi_event_decode;
        snd_strerror;
    local: *;
};

ALSA_0.9.0rc4 {
    global:
        snd_pcm_hw_params_get_channels_min;
        snd_pcm_hw_params_set_buffer_size_near;
        snd_pcm_hw_params_set_period_size_near;
        snd_pcm_hw_params_set_rate_near;
    local: *;
};
CEOF
  gcc -shared -fPIC -Wl,--version-script=/tmp/alsa_stub.ver -o "$LIBDIR/libasound.so.2" /tmp/alsa_stub.c
  echo "    libasound.so.2"

  # --- X11 extension stubs (libXfixes, libXdamage, libXcomposite, libXrandr) ---
  cat > /tmp/xstubs_stub.c << 'CEOF'
#include <stddef.h>
#include <stdint.h>
typedef void* gpointer;
typedef unsigned long XID;

int XFixesQueryExtension(gpointer dpy, int* event_base, int* error_base){return 0;}
int XFixesQueryVersion(gpointer dpy, int* major, int* minor){return 0;}
gpointer XFixesCreateRegion(gpointer dpy, void* rectangles, int nrectangles){return NULL;}
void XFixesDestroyRegion(gpointer dpy, gpointer region){}
void XFixesSetWindowShapeRegion(gpointer dpy, XID win, int shape_kind, int x_off, int y_off, gpointer region){}
void XFixesSelectCursorInput(gpointer dpy, XID win, unsigned long mask){}
gpointer XFixesGetCursorImage(gpointer dpy){return NULL;}
void XFixesFetchRegionAndBounds(gpointer dpy, gpointer region, void* bounds){
    if(bounds){int* b=(int*)bounds; b[0]=b[1]=b[2]=b[3]=0;}
}

int XDamageQueryExtension(gpointer dpy, int* event_base, int* error_base){return 0;}
XID XDamageCreate(gpointer dpy, XID drawable, int level){return 0;}
void XDamageDestroy(gpointer dpy, XID damage){}
void XDamageSubtract(gpointer dpy, XID damage, XID repair, XID parts){}

int XCompositeQueryExtension(gpointer dpy, int* event_base, int* error_base){return 0;}
int XCompositeQueryVersion(gpointer dpy, int* major, int* minor){return 0;}
void XCompositeRedirectWindow(gpointer dpy, XID win, int update){}
void XCompositeRedirectSubwindows(gpointer dpy, XID win, int update){}
void XCompositeUnredirectWindow(gpointer dpy, XID win, int update){}
XID XCompositeNameWindowPixmap(gpointer dpy, XID win){return 0;}

int XRRQueryExtension(gpointer dpy, int* event_base, int* error_base){return 0;}
int XRRQueryVersion(gpointer dpy, int* major, int* minor){return 0;}
void XRRSelectInput(gpointer dpy, XID win, int mask){}
int XRRUpdateConfiguration(gpointer event){return 0;}
gpointer XRRGetScreenInfo(gpointer dpy, XID root){return NULL;}
void XRRFreeScreenConfigInfo(gpointer config){}
gpointer XRRGetScreenResources(gpointer dpy, XID win){return NULL;}
void XRRFreeScreenResources(gpointer res){}
CEOF
  gcc -shared -fPIC -o "$LIBDIR/libXfixes.so.3" /tmp/xstubs_stub.c
  gcc -shared -fPIC -o "$LIBDIR/libXdamage.so.1" /tmp/xstubs_stub.c
  gcc -shared -fPIC -o "$LIBDIR/libXcomposite.so.1" /tmp/xstubs_stub.c
  gcc -shared -fPIC -o "$LIBDIR/libXrandr.so.2" /tmp/xstubs_stub.c
  echo "    libXfixes.so.3, libXdamage.so.1, libXcomposite.so.1, libXrandr.so.2"

  # Register stubs with ldconfig so system can find them
  sudo bash -c 'echo /home/opc/lib > /etc/ld.so.conf.d/vm-stubs.conf && /sbin/ldconfig' 2>/dev/null || true
  echo "  GCC stubs compiled and registered."

  touch "$SETUP_MARKER"
  echo "Chromium setup complete."
else
  echo "Chromium setup already done (marker exists)."
fi

# Set LD_LIBRARY_PATH so linker finds stubs even if ldconfig path isn't checked first.
if ls /home/opc/lib/*.so* 2>/dev/null | grep -q .; then
  export LD_LIBRARY_PATH="/home/opc/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  echo "Stubs present → LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
fi

CHROME_BIN=$(find /home/opc/.cache/ms-playwright -name "chrome-headless-shell" -type f 2>/dev/null | head -1)
[ -z "$CHROME_BIN" ] && echo "WARN: chromium binary not found after setup"

# ── Step 1: Download CSVs from CBOS ─────────────────────────────────────────
# NOTE: Old CSVs are kept until AFTER download succeeds.
# mo_downloader.py overwrites each CSV in place when it downloads a new one.
# Deleting first = losing yesterday's data if today's download fails for that client.
# VM has Indian IP — can reach backoffice.motilaloswal.com
if [ "$IS_FULL" = "true" ]; then
  echo "Running FULL history download..."
  python3 mo_downloader.py --full --downloads-only
else
  echo "Running incremental download..."
  python3 mo_downloader.py --downloads-only
fi

# ── Step 2: Import CSVs → trades.json ───────────────────────────────────────
echo "Importing CSVs..."
python3 import_all.py

# ── Step 3: Push trades.json + ledger.json to repo via GitHub API ───────────
# No git binary needed — uses Python + urllib (stdlib only)
echo "Pushing data files to repo..."
python3 - <<'PYEOF'
import json, base64, urllib.request, os, sys

TOKEN = os.environ.get('GITHUB_TOKEN') or json.load(open('/home/opc/app/bot_config.json', encoding='utf-8-sig')).get('github_token', '')
REPO  = 'jainrishank20/client-tracker-mofsl'
API   = f'https://api.github.com/repos/{REPO}/contents'
FILES = ['trades.json', 'ledger.json', 'open_positions_snapshot.json', 'ticker_overrides.json']
DIR   = '/home/opc/app'

pushed = 0
for fname in FILES:
    fpath = os.path.join(DIR, fname)
    if not os.path.exists(fpath):
        continue
    with open(fpath, 'rb') as f:
        raw = f.read()
    content_b64 = base64.b64encode(raw).decode()

    # Get current SHA (needed for update)
    url = f'{API}/{fname}'
    req = urllib.request.Request(url, headers={
        'Authorization': f'token {TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'vm-runner'
    })
    try:
        resp = urllib.request.urlopen(req)
        sha = json.loads(resp.read().decode()).get('sha')
    except Exception:
        sha = None

    body = {'message': f'chore: update {fname} from VM download [skip ci]',
            'content': content_b64, 'branch': 'main'}
    if sha:
        body['sha'] = sha

    req = urllib.request.Request(url,
        data=json.dumps(body).encode(),
        headers={'Authorization': f'token {TOKEN}',
                 'Content-Type': 'application/json',
                 'Accept': 'application/vnd.github.v3+json',
                 'User-Agent': 'vm-runner'},
        method='PUT')
    try:
        urllib.request.urlopen(req)
        print(f'  Pushed {fname}')
        pushed += 1
    except Exception as e:
        print(f'  WARN: failed to push {fname}: {e}', file=sys.stderr)

print(f'Done — pushed {pushed}/{len(FILES)} files to repo.')
PYEOF

CSV_COUNT=$(ls /home/opc/app/mo_csvs/TradeDetailsAndSummary_*_2026_2027.csv 2>/dev/null | wc -l)
echo "CSVs in mo_csvs after download: ${CSV_COUNT}"

# ── Step 4: Trigger GHA with skip_download=true ─────────────────────────────
# GHA handles: GSheet sync (needs gsheet_key secret) + Telegram notification
HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  -H "Authorization: token ${GITHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"ref\":\"main\",\"inputs\":{\"skip_download\":\"true\",\"full_history\":\"${IS_FULL}\",\"csv_count\":\"${CSV_COUNT}\"}}" \
  "https://api.github.com/repos/${REPO}/actions/workflows/daily_run.yml/dispatches")
echo "Triggered GHA (skip_download=true) — HTTP $HTTP"
if [ "$HTTP" != "204" ]; then
  echo "ERROR: GHA workflow dispatch failed (HTTP $HTTP)"
  exit 1
fi

echo "=== Done $(date '+%Y-%m-%d %H:%M:%S') ==="
} >> "$LOG" 2>&1
