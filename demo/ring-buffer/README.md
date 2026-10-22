# ring_buffer - CodeSentry demo repository

A deliberately small Rust crate used to exercise CodeSentry end to end.

`RingBuffer::drain` has an off-by-one bug on the wrap-around path: when the
buffered range crosses the end of the backing storage it returns one byte too
few. The committed tests in `tests/basic.rs` only cover the non-wrapping path,
so `cargo build` and `cargo test` are both green and the bug survives.

That is the point: it gives CodeSentry's test generator something real to find.

## Using it

1. Push this directory to a GitHub repository you own.
2. In the CodeSentry shell:

   ```
   add <owner>/ring-buffer
   set config <owner>/ring-buffer ../configs/cargo-main-auto-tests.json
   enable <owner>/ring-buffer
   ```

3. Open a pull request against `main` that touches `src/lib.rs`, then:

   ```
   review <owner>/ring-buffer <pr_number>
   ```

The build and the suite pass, so the review continues to intent validation and
test generation. The generated wrap-around test fails against the branch, and
the review comment reports it in full rather than opening a pull request for it
- a red generated test is either a real defect or a bad test, and that call
belongs to a human.

## Reproducing the bug by hand

```rust
let mut buffer = RingBuffer::with_capacity(3);
buffer.push(1);
buffer.push(2);
buffer.drain();        // head is now at index 2
buffer.push(10);
buffer.push(20);       // this range wraps
assert_eq!(buffer.drain(), vec![10, 20]); // fails: returns vec![10]
```
