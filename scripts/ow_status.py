name: OW2 Status Monitor (Enhanced)

on:
  schedule:
    - cron: "0 * * * *"  # Stündlich
    - cron: "*/15 * * * *"  # Alle 15min für kritische Checks (optional)
  workflow_dispatch:
    inputs:
      force_alert:
        description: 'Force alert notification'
        required: false
        type: boolean
        default: false

permissions:
  contents: write

jobs:
  status_check:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    
    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0  # Für Git-History

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: 'pip'

      - name: Install dependencies
        run: |
          pip install --upgrade pip
          pip install requests beautifulsoup4 pillow

      - name: Create asset directories
        run: mkdir -p .bot_state assets

      - name: Run status check
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          THUMB_URL: https://raw.githubusercontent.com/${{ github.repository }}/main/assets/ow2.png
          ALERT_ROLE_ID: ${{ secrets.ALERT_ROLE_ID }}
          REGION_ROLE_IDS: ${{ secrets.REGION_ROLE_IDS }}
          # Schwellwerte
          INFO_MS: "200"
          WARN_MS: "400"
          CRITICAL_MS: "800"
          JITTER_WARN_MS: "100"
          CERT_WARN_DAYS: "14"
          # Alert-Konfiguration
          ALERT_COOLDOWN_HOURS: "2"
          ESCALATION_MINUTES: "30"
          GITHUB_REPOSITORY: ${{ github.repository }}
        run: python scripts/ow_status.py
        continue-on-error: false

      - name: Upload artifacts on failure
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: debug-logs-${{ github.run_number }}
          path: |
            .bot_state/*.json
            assets/*.png
          retention-days: 7

      - name: Commit state changes
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add .bot_state/*.txt .bot_state/*.json assets/*.png || true
          git diff --staged --quiet || git commit -m "🤖 Update OW2 status [skip ci]"
          git push || echo "No changes to push"

      - name: Check for persistent issues
        id: health_check
        run: |
          # Prüfe ob mehr als 3 Fehler in letzten 24h
          if [ -f .bot_state/history.json ]; then
            FAIL_COUNT=$(jq '[.[-24:][].ok] | map(select(. == 0)) | length' .bot_state/history.json)
            if [ "$FAIL_COUNT" -gt 3 ]; then
              echo "warning=High failure rate detected: $FAIL_COUNT/24" >> $GITHUB_OUTPUT
            fi
          fi

      - name: Create issue on persistent failure
        if: steps.health_check.outputs.warning != ''
        uses: actions/github-script@v7
        with:
          script: |
            const warning = '${{ steps.health_check.outputs.warning }}';
            const issues = await github.rest.issues.listForRepo({
              owner: context.repo.owner,
              repo: context.repo.repo,
              state: 'open',
              labels: 'monitoring-alert'
            });
            
            if (issues.data.length === 0) {
              await github.rest.issues.create({
                owner: context.repo.owner,
                repo: context.repo.repo,
                title: '⚠️ OW2 Status: Persistent Issues Detected',
                body: `${warning}\n\nCheck the [status dashboard](https://github.com/${context.repo.owner}/${context.repo.repo}/actions) for details.`,
                labels: ['monitoring-alert', 'automated']
              });
            }

  # Wöchentlicher Report (optional)
  weekly_report:
    runs-on: ubuntu-latest
    if: github.event.schedule == '0 0 * * 0'  # Sonntags um Mitternacht
    
    steps:
      - uses: actions/checkout@v4
      
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install requests pillow

      - name: Generate weekly report
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          GITHUB_REPOSITORY: ${{ github.repository }}
        run: python scripts/weekly_report.py

      - name: Commit report
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add assets/weekly_report_*.png || true
          git commit -m "📊 Weekly report [skip ci]" || echo "No changes"
          git push || true