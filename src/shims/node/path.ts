export const dirname = () => ''
export const normalize = (value: string) => value
export const join = (...segments: string[]) => segments.join('/')

export default {
  dirname,
  normalize,
  join,
}
