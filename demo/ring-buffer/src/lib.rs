//! A fixed-capacity ring buffer.
//!
//! Demo crate for CodeSentry: `drain` has an off-by-one bug on the wrap-around
//! path that the committed tests do not exercise.

#[derive(Debug)]
pub struct RingBuffer {
    slots: Vec<u8>,
    head: usize,
    len: usize,
}

impl RingBuffer {
    /// Create a buffer holding at most `capacity` bytes.
    ///
    /// # Panics
    /// Panics when `capacity` is zero.
    pub fn with_capacity(capacity: usize) -> Self {
        assert!(capacity > 0, "capacity must be greater than zero");
        Self {
            slots: vec![0; capacity],
            head: 0,
            len: 0,
        }
    }

    pub fn capacity(&self) -> usize {
        self.slots.len()
    }

    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    pub fn is_full(&self) -> bool {
        self.len == self.slots.len()
    }

    /// Append a byte. Returns `false` when the buffer is full.
    pub fn push(&mut self, value: u8) -> bool {
        if self.is_full() {
            return false;
        }
        let tail = (self.head + self.len) % self.slots.len();
        self.slots[tail] = value;
        self.len += 1;
        true
    }

    /// Remove and return every buffered byte, oldest first.
    pub fn drain(&mut self) -> Vec<u8> {
        let capacity = self.slots.len();
        let mut out = Vec::with_capacity(self.len);

        // BUG: `self.len - 1` drops the last byte whenever the buffered range
        // wraps past the end of the backing storage. Correct bound is `self.len`.
        let count = if self.head + self.len > capacity {
            self.len - 1
        } else {
            self.len
        };

        for offset in 0..count {
            out.push(self.slots[(self.head + offset) % capacity]);
        }

        self.head = (self.head + self.len) % capacity;
        self.len = 0;
        out
    }
}
