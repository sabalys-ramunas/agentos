import { isFailedLine, parseFailures, splitLines } from './failures';

const lines = (text: string) => splitLines(text);

describe('parseFailures', () => {
  it('reads pytest: the short summary, with the file, line and message from each section', () => {
    const out = lines(`============================= test session starts ==============================
collected 214 items

tests/test_orders.py ....F.....                                          [ 18%]
tests/test_stock.py ..F.                                                 [ 41%]

=================================== FAILURES ===================================
_____________________________ test_refund_webhook ______________________________

    def test_refund_webhook():
>       assert total == 42
E       assert 41.99 == 42

tests/test_orders.py:88: AssertionError
__________________________ TestStock.test_reserve[2] ___________________________

self = <tests.test_stock.TestStock object at 0x7f>

>       stock.reserve("sku-1", 2)
E       KeyError: 'sku'

tests/test_stock.py:41: KeyError
=========================== short test summary info ============================
FAILED tests/test_orders.py::test_refund_webhook - assert 41.99 == 42
FAILED tests/test_stock.py::TestStock::test_reserve[2] - KeyError: 'sku'
==================== 2 failed, 211 passed in 48.20s ====================
`);
    expect(parseFailures('pytest', out, [])).toEqual([
      { name: 'test_refund_webhook', file: 'tests/test_orders.py', line: 88, message: 'assert 41.99 == 42', at: { stream: 'stdout', line: 7 } },
      { name: 'TestStock::test_reserve[2]', file: 'tests/test_stock.py', line: 41, message: "KeyError: 'sku'", at: { stream: 'stdout', line: 14 } },
    ]);
  });

  it('reads pytest sections when there is no short summary', () => {
    const out = lines(`=================================== FAILURES ===================================
___________________________________ test_one ___________________________________
E       assert 1 == 2
tests/test_a.py:3: AssertionError
1 failed in 0.02s`);
    expect(parseFailures('pytest', out, [])).toEqual([
      { name: 'test_one', file: 'tests/test_a.py', line: 3, message: 'assert 1 == 2', at: { stream: 'stdout', line: 1 } },
    ]);
  });

  it('reads vitest detail blocks, with colour codes in the output', () => {
    const out = lines(` \u001b[31m❯\u001b[39m src/checkout/payment.test.ts (12 tests | 2 failed) 340ms

⎯⎯⎯⎯⎯⎯⎯ Failed Tests 2 ⎯⎯⎯⎯⎯⎯⎯

 \u001b[41mFAIL\u001b[49m  src/checkout/payment.test.ts > payment > rounds the total to cents before it charges
AssertionError: expected 41.999999 to be 42 // Object.is equality
 ❯ src/checkout/payment.test.ts:88:31
     86|   const total = charge(cart)
     88|   expect(total).toBe(42)

⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/2]⎯

 FAIL  src/checkout/payment.test.ts > payment > shows the card error next to the field
TestingLibraryElementError: Unable to find role="alert"
 ❯ src/checkout/payment.test.ts:120:12
`);
    expect(parseFailures('vitest', out, [])).toEqual([
      {
        name: 'payment › rounds the total to cents before it charges',
        file: 'src/checkout/payment.test.ts',
        line: 88,
        message: 'AssertionError: expected 41.999999 to be 42 // Object.is equality',
        at: { stream: 'stdout', line: 4 },
      },
      {
        name: 'payment › shows the card error next to the field',
        file: 'src/checkout/payment.test.ts',
        line: 120,
        message: 'TestingLibraryElementError: Unable to find role="alert"',
        at: { stream: 'stdout', line: 12 },
      },
    ]);
  });

  it('reads the vitest tree when the detail blocks are not there', () => {
    const out = lines(` ✓ src/cart/total.test.ts (14 tests) 41ms
 ❯ src/checkout/payment.test.ts (12 tests | 2 failed) 340ms
   × rounds the total to cents before it charges 12ms
   × shows the card error next to the field 30ms
 ✓ src/catalog/search.test.ts (61 tests) 410ms`);
    expect(parseFailures('vitest', out, [])).toEqual([
      { name: 'rounds the total to cents before it charges', file: 'src/checkout/payment.test.ts', at: { stream: 'stdout', line: 2 } },
      { name: 'shows the card error next to the field', file: 'src/checkout/payment.test.ts', at: { stream: 'stdout', line: 3 } },
    ]);
  });

  it('reads jest from stderr, where it writes', () => {
    const err = lines(`FAIL src/cart/total.test.ts
  cartTotal
    ✓ adds lines (3 ms)
    ✕ applies a percentage discount per line (5 ms)

  ● cartTotal › applies a percentage discount per line

    expect(received).toBe(expected) // Object.is equality

    Expected: 41.99
    Received: 42

      at Object.<anonymous> (src/cart/total.test.ts:38:33)

Tests:       1 failed, 12 passed, 13 total`);
    expect(parseFailures('jest', [], err)).toEqual([
      {
        name: 'cartTotal › applies a percentage discount per line',
        file: 'src/cart/total.test.ts',
        line: 38,
        message: 'expect(received).toBe(expected) // Object.is equality',
        at: { stream: 'stderr', line: 5 },
      },
    ]);
  });

  it('reads swift test, XCTest and Swift Testing', () => {
    const out = lines(`Test Case '-[AcmeKitTests.CartTests testRounding]' started.
/Users/agent/src/AcmeKit/Tests/AcmeKitTests/CartTests.swift:38: error: -[AcmeKitTests.CartTests testRounding] : XCTAssertEqual failed: ("42.0") is not equal to ("41.99")
Test Case '-[AcmeKitTests.CartTests testRounding]' failed (0.003 seconds).
✘ Test roundsVATPerLine() recorded an issue at VATTests.swift:51:5: Expectation failed: (vat → 8.39) == 8.4
✘ Test run with 3 tests failed after 0.1 seconds with 1 issue.
Executed 12 tests, with 1 failure (0 unexpected) in 0.05 (0.06) seconds`);
    expect(parseFailures('swift', out, [])).toEqual([
      {
        name: 'CartTests.testRounding',
        file: 'Tests/AcmeKitTests/CartTests.swift',
        line: 38,
        message: 'XCTAssertEqual failed: ("42.0") is not equal to ("41.99")',
        at: { stream: 'stdout', line: 1 },
      },
      { name: 'roundsVATPerLine()', file: 'VATTests.swift', line: 51, message: 'Expectation failed: (vat → 8.39) == 8.4', at: { stream: 'stdout', line: 3 } },
    ]);
  });

  it('reads go test, keeping the subtests that say where', () => {
    const out = lines(`=== RUN   TestRefund
=== RUN   TestRefund/zero
    refund_test.go:42: expected 41.99, got 42
--- FAIL: TestRefund (0.00s)
    --- FAIL: TestRefund/zero (0.00s)
        refund_test.go:42: expected 41.99, got 42
--- FAIL: TestStock (0.01s)
    stock_test.go:17: reserve: sku missing
FAIL
FAIL	example.com/shop/orders	0.012s`);
    expect(parseFailures('go', out, [])).toEqual([
      { name: 'TestRefund/zero', file: 'refund_test.go', line: 42, message: 'expected 41.99, got 42', at: { stream: 'stdout', line: 4 } },
      { name: 'TestStock', file: 'stock_test.go', line: 17, message: 'reserve: sku missing', at: { stream: 'stdout', line: 6 } },
    ]);
  });

  it('finds nothing in output it does not recognise, and never guesses', () => {
    const out = lines('Would copy 1,204 files (3.2 GB) to /backups/photos\nsomething failed somewhere\nNothing was copied: dry run.');
    for (const runner of ['pytest', 'vitest', 'jest', 'swift', 'go'] as const) expect(parseFailures(runner, out, out)).toEqual([]);
  });

  it('reads the same failure once when both streams carry it', () => {
    const block = lines(' FAIL  a.test.ts > one\nError: no\n ❯ a.test.ts:3:1');
    expect(parseFailures('vitest', block, block)).toHaveLength(1);
  });
});

describe('isFailedLine', () => {
  it.each([
    '   × rounds the total 12ms',
    ' ❯ src/checkout/payment.test.ts (12 tests | 2 failed) 340ms',
    'FAILED tests/test_orders.py::test_refund - assert 1 == 2',
    'E       assert 41.99 == 42',
    '--- FAIL: TestRefund (0.00s)',
    '  ● cartTotal › applies a discount',
    'AssertionError: expected 41.999999 to be 42',
    '\u001b[31m FAIL \u001b[39m src/a.test.ts > one',
  ])('marks %j', (line) => expect(isFailedLine(line)).toBe(true));

  it.each([' ✓ src/cart/total.test.ts (14 tests) 41ms', '211 passed, 3 skipped in 48.20s', 'collected 214 items', ''])('leaves %j', (line) =>
    expect(isFailedLine(line)).toBe(false),
  );
});
