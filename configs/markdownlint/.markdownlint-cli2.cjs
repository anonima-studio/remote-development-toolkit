module.exports = {
  config: {
    default: true,
    MD013: false,
    MD029: false,
  },
  globs: [
    '../../**/*.md',
  ],
  ignores: [
    '../../**/node_modules/**',
    '../../artifacts/traces/**',
    '../../**/.output/**',
    '../../**/.nuxt/**',
    '../../**/.next/**',
    '../../**/dist/**',
    '../../**/turbo/**',
  ],
};
