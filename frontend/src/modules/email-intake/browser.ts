// Leaving the console for Microsoft's sign-in page. One seam, so a test can
// see where the browser would have gone without jsdom navigating.
export const browser = {
  assign(url: string): void {
    window.location.assign(url);
  },
};
