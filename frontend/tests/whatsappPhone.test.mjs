import test from 'node:test'
import assert from 'node:assert/strict'
import { isInternationalPhone } from '../src/lib/whatsappPhone.ts'

test('accepts international numbers in E.164-style format', () => {
  assert.equal(isInternationalPhone('+2348012345678'), true)
  assert.equal(isInternationalPhone('+12025550123'), true)
  assert.equal(isInternationalPhone(' +442079460123 '), true)
})

test('rejects local, malformed, and out-of-range numbers', () => {
  for (const value of ['08012345678', '+012345678', '+1234567', '+1234567890123456', '+234 8012345678', '']) {
    assert.equal(isInternationalPhone(value), false, value)
  }
})
