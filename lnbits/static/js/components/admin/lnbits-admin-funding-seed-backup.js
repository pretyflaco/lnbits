window.app.component('lnbits-admin-funding-seed-backup', {
  props: ['active', 'is-super-user', 'form-data', 'settings'],
  template: '#lnbits-admin-funding-seed-backup',
  data() {
    return {
      dialog: {
        show: false,
        step: 1,
        mode: 'add',
        completed: false,
        seed: '',
        visible: false,
        challenge: [],
        answers: {},
        error: '',
        confirmField: '',
        seedField: ''
      }
    }
  },
  watch: {
    active(isActive) {
      if (isActive) {
        this.openIfRequired()
      }
    },
    'formData.lnbits_backend_wallet_class'(walletClass, previousWalletClass) {
      // Blink handles its own dialog triggers in the seed-field watcher
      if (walletClass === 'BlinkNonCustodialWallet') return
      const source = this.seedBackupSource(walletClass)
      if (previousWalletClass && source && this.formData[source.seedField]) {
        this.formData[source.confirmField] = false
      }
      this.openIfRequired()
    },
    'formData.boltz_mnemonic'() {
      this.formData.boltz_mnemonic_backup_confirmed =
        this.formData.boltz_mnemonic === this.settings.boltz_mnemonic
          ? this.settings.boltz_mnemonic_backup_confirmed
          : false
      this.openIfRequired()
    },
    'formData.phoenixd_mnemonic'() {
      this.formData.phoenixd_mnemonic_backup_confirmed =
        this.formData.phoenixd_mnemonic === this.settings.phoenixd_mnemonic
          ? this.settings.phoenixd_mnemonic_backup_confirmed
          : false
      this.openIfRequired()
    },
    'formData.spark_l2_mnemonic'() {
      this.formData.spark_l2_mnemonic_backup_confirmed =
        this.formData.spark_l2_mnemonic === this.settings.spark_l2_mnemonic
          ? this.settings.spark_l2_mnemonic_backup_confirmed
          : false
      this.openIfRequired()
    },
    'formData.blink_noncustodial_spark_mnemonic'() {
      this.onBlinkSeedChange()
    }
  },
  computed: {
    seedWords() {
      return this.dialog.seed
        .split(/\s+/)
        .filter(Boolean)
        .map((word, index) => ({index, word}))
    }
  },
  created() {
    this.openIfRequired()
  },
  methods: {
    seedBackupSource(walletClass = this.formData.lnbits_backend_wallet_class) {
      if (walletClass === 'BoltzWallet') {
        return {
          seedField: 'boltz_mnemonic',
          confirmField: 'boltz_mnemonic_backup_confirmed'
        }
      }
      if (walletClass === 'PhoenixdWallet') {
        return {
          seedField: 'phoenixd_mnemonic',
          confirmField: 'phoenixd_mnemonic_backup_confirmed'
        }
      }
      if (walletClass === 'SparkL2Wallet') {
        return {
          seedField: 'spark_l2_mnemonic',
          confirmField: 'spark_l2_mnemonic_backup_confirmed'
        }
      }
      if (walletClass === 'BlinkNonCustodialWallet') {
        return {
          seedField: 'blink_noncustodial_spark_mnemonic',
          confirmField: 'blink_noncustodial_mnemonic_backup_confirmed'
        }
      }
    },
    openIfRequired() {
      if (!this.active || !this.isSuperUser) return
      // Blink: no popup on navigation; the dialog is guarded by the
      // seed-field watcher instead (see onBlinkSeedChange)
      if (
        this.formData.lnbits_backend_wallet_class === 'BlinkNonCustodialWallet'
      )
        return

      const source = this.seedBackupSource()
      if (!source) return

      const seed = (this.formData[source.seedField] || '').trim()
      const confirmed = this.formData[source.confirmField]
      if (!seed || confirmed || this.dialog.show) return

      this.dialog = {
        show: true,
        step: 1,
        mode: 'add',
        completed: false,
        seed,
        visible: false,
        challenge: [],
        answers: {},
        error: '',
        confirmField: source.confirmField,
        seedField: ''
      }
    },
    onBlinkSeedChange() {
      const seedField = 'blink_noncustodial_spark_mnemonic'
      const confirmField = 'blink_noncustodial_mnemonic_backup_confirmed'
      if (this.dialog.show) return

      const stored = (this.settings[seedField] || '').trim()
      const current = (this.formData[seedField] || '').trim()

      if (current === stored) {
        // untouched: keep the persisted confirmation state
        this.formData[confirmField] = this.settings[confirmField]
        return
      }
      this.formData[confirmField] = false
      if (!current && !stored) return

      this.dialog = {
        show: true,
        step: 1,
        mode: current ? 'add' : 'remove',
        completed: false,
        seed: current || stored,
        visible: false,
        challenge: [],
        answers: {},
        error: '',
        confirmField,
        seedField
      }
    },
    onDialogHide() {
      // dialog dismissed without completing verification: revert the
      // field to the saved value so nothing is applied or wiped
      if (this.dialog.completed || !this.dialog.seedField) return
      this.formData[this.dialog.seedField] =
        this.settings[this.dialog.seedField]
    },
    prepareChallenge() {
      const words = this.dialog.seed.split(/\s+/).filter(Boolean)
      const count = Math.min(4, words.length)
      const indexes = _.shuffle([...Array(words.length).keys()]).slice(0, count)
      this.dialog.challenge = indexes
        .sort((a, b) => a - b)
        .map(index => ({index, word: words[index]}))
      this.dialog.answers = {}
      this.dialog.error = ''
      this.dialog.step = 2
    },
    submitChallenge() {
      const isValid = this.dialog.challenge.every(({index, word}) => {
        const answer = this.dialog.answers[index] || ''
        return answer.trim().toLowerCase() === word.toLowerCase()
      })
      if (!isValid) {
        this.dialog.error =
          'One or more words are incorrect. Check your backup and try again.'
        return
      }

      const field = this.dialog.confirmField
      LNbits.api
        .request(
          'PATCH',
          '/admin/api/v1/settings',
          this.g.user.wallets[0].adminkey,
          {
            [field]: true
          }
        )
        .then(() => {
          this.dialog.completed = true
          this.formData[field] = true
          this.settings[field] = true
          this.dialog.show = false
          Quasar.Notify.create({
            type: 'positive',
            message: 'Seed backup confirmed',
            icon: 'check'
          })
        })
        .catch(LNbits.utils.notifyApiError)
    }
  }
})
