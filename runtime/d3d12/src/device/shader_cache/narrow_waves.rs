//! A second translation for programs that need 64-lane waves on a host without them.
use super::{compile, portable_wave_range, source_digest};
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, LazyLock, Mutex, MutexGuard};

/// The console program behind each translation that requires 64-lane waves,
/// keyed by the translation's digest. Few programs need one.
static SOURCES: LazyLock<Mutex<HashMap<String, Arc<[u8]>>>> = LazyLock::new(Default::default);

fn sources() -> MutexGuard<'static, HashMap<String, Arc<[u8]>>> {
    SOURCES.lock().unwrap_or_else(|error| error.into_inner())
}

/// Note the compiler input of a compute translation that may need replacing.
pub(super) fn remember(translation: &[u8], shader: &[u8]) {
    if portable_wave_range(translation) == Some((64, 64))
        && let Some(key) = source_digest(translation)
    {
        sources().entry(key).or_insert_with(|| shader.into());
    }
}

/// Ask the compiler to translate the program behind `translation` for 32-lane
/// waves. `None` leaves the caller to refuse the ordinary translation as before.
pub(crate) fn narrow_wave_variant(translation: &[u8]) -> Option<(Vec<u8>, String)> {
    let shader = sources().get(&source_digest(translation)?)?.clone();
    let source = source_digest(&shader)?;
    let variant = compile::narrow_wave(&shader, &source)?;
    if portable_wave_range(&variant) == Some((64, 64)) {
        return None;
    }
    static LOADED: LazyLock<Mutex<HashSet<String>>> = LazyLock::new(Default::default);
    if LOADED.lock().unwrap_or_else(|error| error.into_inner()).insert(source.clone()) {
        crate::diagnostics::log(&format!("narrow-wave compute shader loaded, source SHA-256 {source}"));
    }
    Some((variant, format!("{source}_w32")))
}
