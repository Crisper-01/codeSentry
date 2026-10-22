//! The coverage this demo repository ships with: only the non-wrapping path.

use ring_buffer::RingBuffer;

#[test]
fn a_new_buffer_is_empty() {
    let buffer = RingBuffer::with_capacity(4);
    assert!(buffer.is_empty());
    assert_eq!(buffer.len(), 0);
    assert_eq!(buffer.capacity(), 4);
}

#[test]
fn pushing_then_draining_returns_the_bytes_in_order() {
    let mut buffer = RingBuffer::with_capacity(4);
    assert!(buffer.push(1));
    assert!(buffer.push(2));
    assert!(buffer.push(3));

    assert_eq!(buffer.drain(), vec![1, 2, 3]);
    assert!(buffer.is_empty());
}

#[test]
fn a_full_buffer_refuses_more_bytes() {
    let mut buffer = RingBuffer::with_capacity(2);
    assert!(buffer.push(1));
    assert!(buffer.push(2));
    assert!(!buffer.push(3));
    assert!(buffer.is_full());
}
