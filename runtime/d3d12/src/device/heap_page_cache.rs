//! Bounded weak lookup of exact live page imports. Ownership remains with resources.
use std::{collections::HashMap, sync::{Arc, Weak}};

const LIMIT: usize = 4096;

pub(super) struct PageCache<T>(HashMap<u64, Weak<T>>);

impl<T> Default for PageCache<T> {
    fn default() -> Self { Self(HashMap::new()) }
}

impl<T> PageCache<T> {
    pub(super) fn get_or_open<E>(&mut self, base: u64, open: impl FnOnce() -> Result<Arc<T>, E>) -> Result<Arc<T>, E> {
        if let Some(value) = self.0.get(&base).and_then(Weak::upgrade) {
            return Ok(value);
        }
        // Do not retain an unsuccessful open or any strong ownership.
        let value = open()?;
        if self.0.len() >= LIMIT {
            self.0.retain(|_, entry| entry.strong_count() != 0);
        }
        if self.0.len() < LIMIT {
            self.0.insert(base, Arc::downgrade(&value));
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
        let a = cache.get_or_open(0, || Ok::<_, ()>(Arc::new(7))).unwrap();
        let b = cache.get_or_open(65536, || Ok::<_, ()>(Arc::new(9))).unwrap();
        let same = cache.get_or_open(0, || Err(())).unwrap();
        assert!(Arc::ptr_eq(&a, &same));
        assert!(!Arc::ptr_eq(&b, &same));
        drop(a); drop(same);
        assert_eq!(*cache.get_or_open(0, || Ok::<_, ()>(Arc::new(11))).unwrap(), 11);
    }

    #[test]
    fn failed_imports_are_not_cached_and_live_entries_are_bounded() {
        let mut cache = PageCache::default();
        assert!(cache.get_or_open(0, || Err::<Arc<u32>, _>(())).is_err());
        assert!(cache.0.is_empty());
        let live: Vec<_> = (0..=LIMIT).map(|n| cache.get_or_open(n as u64, || Ok::<_, ()>(Arc::new(n as u32))).unwrap()).collect();
        assert_eq!(cache.0.len(), LIMIT);
        drop(live);
        cache.get_or_open(99999, || Ok::<_, ()>(Arc::new(1))).unwrap();
        assert_eq!(cache.0.len(), 1);
    }
}
