//! Bounded weak lookup of exact live page imports. Ownership remains with resources.
use std::{collections::{HashMap, VecDeque}, sync::{Arc, Weak}};

const LIMIT: usize = 4096;
const SWEEP: usize = 16;

pub(super) struct PageCache<T> {
    entries: HashMap<u64, Weak<T>>,
    sweep: VecDeque<u64>,
}

impl<T> Default for PageCache<T> {
    fn default() -> Self { Self { entries: HashMap::new(), sweep: VecDeque::new() } }
}

impl<T> PageCache<T> {
    /// Reuse the live import at `base` while `usable` accepts it. A rejected
    /// entry (for example, one whose pages were since remapped) is replaced.
    pub(super) fn get_or_open<E>(&mut self, base: u64, usable: impl Fn(&T) -> bool, open: impl FnOnce() -> Result<Arc<T>, E>) -> Result<Arc<T>, E> {
        if let Some(value) = self.entries.get(&base).and_then(Weak::upgrade).filter(|value| usable(value)) {
            return Ok(value);
        }
        // Do not retain an unsuccessful open or any strong ownership.
        let value = open()?;
        if let Some(entry) = self.entries.get_mut(&base) {
            // A remapped key must be replaceable even at capacity. Keep
            // exactly one position in the sweep queue for each key.
            *entry = Arc::downgrade(&value);
            return Ok(value);
        }
        if self.entries.len() >= LIMIT {
            // Incremental reclamation: avoid walking 4096 live weak entries
            // for every cache miss while the guest retains all of its pages.
            for _ in 0..SWEEP.min(self.sweep.len()) {
                let Some(key) = self.sweep.pop_front() else { break };
                if self.entries.get(&key).is_some_and(|entry| entry.strong_count() != 0) {
                    self.sweep.push_back(key);
                } else {
                    self.entries.remove(&key);
                }
            }
        }
        if self.entries.len() < LIMIT {
            self.entries.insert(base, Arc::downgrade(&value));
            self.sweep.push_back(base);
        }
        Ok(value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn interleaved_live_pages_reuse_exact_heap_and_expired_pages_reopen() {
        let mut cache = PageCache::default();
        let a = cache.get_or_open(0, |_| true, || Ok::<_, ()>(Arc::new(7))).unwrap();
        let b = cache.get_or_open(65536, |_| true, || Ok::<_, ()>(Arc::new(9))).unwrap();
        let same = cache.get_or_open(0, |_| true, || Err(())).unwrap();
        assert!(Arc::ptr_eq(&a, &same));
        assert!(!Arc::ptr_eq(&b, &same));
        drop(a); drop(same);
        assert_eq!(*cache.get_or_open(0, |_| true, || Ok::<_, ()>(Arc::new(11))).unwrap(), 11);
    }

    #[test]
    fn rejected_live_entry_is_replaced_by_a_new_import() {
        let mut cache = PageCache::default();
        let old = cache.get_or_open(0, |_| true, || Ok::<_, ()>(Arc::new(1))).unwrap();
        let new = cache.get_or_open(0, |value| *value != 1, || Ok::<_, ()>(Arc::new(2))).unwrap();
        assert_eq!(*new, 2);
        assert!(!Arc::ptr_eq(&old, &new));
        let again = cache.get_or_open(0, |value| *value != 1, || Err(())).unwrap();
        assert!(Arc::ptr_eq(&new, &again));
    }

    #[test]
    fn full_cache_replaces_same_key_without_duplicate_queue_entries() {
        let mut cache = PageCache::default();
        let live: Vec<_> = (0..LIMIT).map(|n| cache.get_or_open(n as u64, |_| true, || Ok::<_, ()>(Arc::new(n))).unwrap()).collect();
        let replacement = cache.get_or_open(0, |_| false, || Ok::<_, ()>(Arc::new(99999))).unwrap();
        assert!(!Arc::ptr_eq(&live[0], &replacement));
        let hit = cache.get_or_open(0, |v| *v == 99999, || Err(())).unwrap();
        assert!(Arc::ptr_eq(&replacement, &hit));
        assert_eq!(cache.entries.len(), LIMIT);
        assert_eq!(cache.sweep.len(), LIMIT);
    }

    #[test]
    fn sweep_reaches_expired_key_behind_live_entries() {
        let mut cache = PageCache::default();
        let mut live: Vec<_> = (0..LIMIT).map(|n| Some(cache.get_or_open(n as u64, |_| true, || Ok::<_, ()>(Arc::new(n))).unwrap())).collect();
        live[LIMIT - 1] = None;
        let mut found = None;
        for n in 0..LIMIT.div_ceil(SWEEP) {
            let base = 100000 + n as u64;
            let value = cache.get_or_open(base, |_| true, || Ok::<_, ()>(Arc::new(n))).unwrap();
            if cache.entries.contains_key(&base) {
                found = Some((base,value));
                break;
            }
        }
        let (base, value) = found.expect("expired key must be reclaimed within one sweep");
        assert!(Arc::ptr_eq(&value, &cache.get_or_open(base, |_| true, || Err(())).unwrap()));
        assert_eq!(cache.entries.len(), LIMIT);
        assert_eq!(cache.sweep.len(), LIMIT);
    }

    #[test]
    fn failed_imports_are_not_cached_and_live_entries_are_bounded() {
        let mut cache = PageCache::default();
        assert!(cache.get_or_open(0, |_| true, || Err::<Arc<u32>, _>(())).is_err());
        assert!(cache.entries.is_empty());
        let live: Vec<_> = (0..=LIMIT).map(|n| cache.get_or_open(n as u64, |_| true, || Ok::<_, ()>(Arc::new(n as u32))).unwrap()).collect();
        assert_eq!(cache.entries.len(), LIMIT);
        drop(live);
        cache.get_or_open(99999, |_| true, || Ok::<_, ()>(Arc::new(1))).unwrap();
        assert_eq!(cache.entries.len(), LIMIT - SWEEP + 1);
        assert_eq!(cache.sweep.len(), cache.entries.len());
    }
}
