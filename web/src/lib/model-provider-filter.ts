interface ProviderWithSlug {
  slug: string;
}

export function filterModelOptionProviders<T extends ProviderWithSlug>(
  providers: T[],
  excludeProviders: string[] = [],
): T[] {
  const excluded = new Set(excludeProviders.map((slug) => slug.toLowerCase()));
  return providers.filter(
    (provider) => !excluded.has(provider.slug.toLowerCase()),
  );
}
