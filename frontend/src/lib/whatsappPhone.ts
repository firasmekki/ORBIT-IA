/** E.164-style international phone number, including a leading +. */
export function isInternationalPhone(value: string): boolean {
  return /^\+[1-9]\d{7,14}$/.test(value.trim())
}
