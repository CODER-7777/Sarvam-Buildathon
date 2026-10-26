// SYNTHETIC demo application. Deliberately contains a loading-state bug.
export async function submitPayment(cartId: string) {
  setPaymentLoading(true);
  try {
    await payments.charge(cartId);
    setPaymentLoading(false);
    navigateToReceipt();
  } catch (error) {
    reportError(error);
    // BUG: loading is never reset on the failure path.
  }
}
