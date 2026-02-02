export class Worker {
  constructor() {
    throw new Error('worker_threads.Worker is not available in the browser.')
  }
}

export default {
  Worker,
}
