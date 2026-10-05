// A view change invalidates reads started for the previous view, even if
// the transport cannot cancel them. Re-selecting the same ID also invalidates.
export class RequestGeneration {
  private generation = 0;

  next(): number {
    return ++this.generation;
  }

  current(): number {
    return this.generation;
  }

  accepts(generation: number): boolean {
    return generation === this.generation;
  }
}
