import { isNotServed } from '../http';

/** A poll that stops once the answer is "not served": asking again will not make the route appear. */
export const pollUnlessNotServed = (ms: number) => (query: { state: { error: Error | null } }) => (isNotServed(query.state.error) ? false : ms);
