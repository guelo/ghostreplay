(function initCOISW() {
  if (typeof window === 'undefined') {
    const addHeaders = (headers) => {
      headers.set('Cross-Origin-Embedder-Policy', 'require-corp')
      headers.set('Cross-Origin-Opener-Policy', 'same-origin')
      return headers
    }

    self.addEventListener('install', () => {
      self.skipWaiting()
    })

    self.addEventListener('activate', (event) => {
      event.waitUntil(self.clients.claim())
    })

    self.addEventListener('message', (event) => {
      if (event.data && event.data.type === 'claim-clients') {
        self.skipWaiting()
        void self.clients.claim()
      }
    })

    self.addEventListener('fetch', (event) => {
      const { request } = event

      if (request.cache === 'only-if-cached' && request.mode !== 'same-origin') {
        return
      }

      event.respondWith(
        (async () => {
          const response = await fetch(request)
          const newHeaders = addHeaders(new Headers(response.headers))

          return new Response(response.body, {
            status: response.status,
            statusText: response.statusText,
            headers: newHeaders,
          })
        })(),
      )
    })

    return
  }

  if (window.crossOriginIsolated || !('serviceWorker' in navigator)) {
    return
  }

  const register = async () => {
    try {
      const registration = await navigator.serviceWorker.register('/coi-serviceworker.js', {
        scope: '/',
        type: 'classic',
      })

      let needsReload = !navigator.serviceWorker.controller

      const activate = () => {
        if (needsReload) {
          window.location.reload()
        }
      }

      if (registration.installing) {
        registration.installing.addEventListener('statechange', (event) => {
          if (event.target?.state === 'activated') {
            activate()
          }
        })
      } else if (registration.active) {
        registration.active.postMessage({ type: 'claim-clients' })
        activate()
      }
    } catch (error) {
      console.warn('Unable to register COOP/COEP service worker', error)
    }
  }

  register()
})()
