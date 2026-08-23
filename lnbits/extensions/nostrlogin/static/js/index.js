window.app = Vue.createApp({
  el: '#vue',
  mixins: [windowMixin],
  data() {
    return {
      connectUri: '',
      sessionId: '',
      status: 'idle',
      authUrl: null,
      pollTimer: null
    }
  },
  computed: {
    statusText() {
      switch (this.status) {
        case 'pending':
          return 'Waiting for approval... approve the request in your signer app.'
        case 'failed':
          return 'Login failed. Please try again.'
        case 'auth_required':
          return 'Additional authentication required.'
        default:
          return ''
      }
    }
  },
  methods: {
    async startSession() {
      try {
        const {data} = await LNbits.api.request(
          'POST',
          '/nostrlogin/api/v1/session',
          null
        )
        this.sessionId = data.id
        this.connectUri = data.connect_uri
        this.status = 'pending'
        this.pollTimer = setInterval(() => this.pollStatus(), 2000)
      } catch (error) {
        LNbits.utils.notifyApiError(error)
        this.statusTextFallback(error)
      }
    },
    async pollStatus() {
      if (!this.sessionId) return
      try {
        const {data} = await LNbits.api.request(
          'GET',
          '/nostrlogin/api/v1/session/' + this.sessionId + '/status',
          null
        )
        this.status = data.status
        if (data.auth_url) {
          this.authUrl = data.auth_url
        }
        if (data.status === 'approved') {
          clearInterval(this.pollTimer)
          window.location.href = data.redirect || '/wallet'
        } else if (data.status === 'failed') {
          clearInterval(this.pollTimer)
          Quasar.Notify.create({
            type: 'negative',
            message: 'Nostr login failed.',
            caption: data.reason || ''
          })
        }
      } catch (error) {
        // 401/404 mean the session is gone; stop polling.
        clearInterval(this.pollTimer)
        this.status = 'failed'
      }
    },
    copyUri() {
      LNbits.utils.copyText(this.connectUri)
    },
    statusTextFallback() {}
  },
  created() {
    this.startSession()
  },
  beforeUnmount() {
    if (this.pollTimer) clearInterval(this.pollTimer)
  }
})
