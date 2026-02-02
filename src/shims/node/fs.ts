export const readFileSync = () => {
  throw new Error('fs.readFileSync is not available in the browser shim.')
}

export default {
  readFileSync,
}
