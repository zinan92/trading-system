# GitHub provenance audit — missing Issue/PR objects #222–#329

## Finding

On 2026-07-24, an authenticated `zinan92` GitHub API check showed that
`main` contains the merge commits below, while their corresponding Issue/PR
objects return HTTP 404. This is a **GitHub metadata gap**, not a code-loss
claim: the commit objects remain reachable from `main` and are the authority
for the delivered code. Missing PR pages cannot be recreated at their original
numbers, so this document is the durable index and [#330](https://github.com/zinan92/trading-system/issues/330)
is the reachable tracking record.

## Missing PR-object index

| Referenced PR | Merge commit | Branch named by the commit |
|---:|---|---|
| #223 | [`4e7bfbe`](https://github.com/zinan92/trading-system/commit/4e7bfbebf9b0986f1c2e6eb647f125c3a95158b9) | `issue-222-reviewed-implementation-plan` |
| #225 | [`7b9ad84`](https://github.com/zinan92/trading-system/commit/7b9ad84a8703a634074ac7116b5899331bb6f964) | `issue-224-dca-aggregate-tp-contract` |
| #228 | [`fd16e08`](https://github.com/zinan92/trading-system/commit/fd16e08c09aecd098cdfe603db3efe577be7c857) | `issue-227-runtime-state-contract` |
| #231 | [`4428533`](https://github.com/zinan92/trading-system/commit/44285334870d5d551cded170467117394bb624e1) | `issue-230-dca-multi-position-exit` |
| #233 | [`0eaa3f6`](https://github.com/zinan92/trading-system/commit/0eaa3f65cf90cd507bdbcdf79e62aea003f43014) | `issue-232-registry-dca-exit` |
| #234 | [`3cb6fb9`](https://github.com/zinan92/trading-system/commit/3cb6fb951d9631c20b8199b3f79e737e1b27dcce) | `issue-229-tick-failure-phases` |
| #236 | [`e029183`](https://github.com/zinan92/trading-system/commit/e029183a39ac8cf297f0c218bc1999798564ffce) | `issue-235-registry-tick-diagnostics` |
| #239 | [`b79b71e`](https://github.com/zinan92/trading-system/commit/b79b71e6204f4b484dec2e54d650c70fe97862fc) | `issue-237-closeout-fault-injection` |
| #240 | [`4093a32`](https://github.com/zinan92/trading-system/commit/4093a326af3e256bcfcaf85bb6115a5f49ecbdd8d) | `issue-238-protective-sweep-fixture` |
| #242 | [`348a4ce`](https://github.com/zinan92/trading-system/commit/348a4cea686e3ab2bda63312aeac17d54abe73e7) | `issue-241-registry-protective-sweep` |
| #244 | [`7527577`](https://github.com/zinan92/trading-system/commit/752757735f9883d4b6de80a991b028a7c8f29502) | `issue-243-paper-dashboard-deploy-receipt` |
| #246 | [`f2b4fd8`](https://github.com/zinan92/trading-system/commit/f2b4fd8e116820336b2f5c440c77f69d83e7863d) | `issue-245-failure-copy-matrix` |
| #248 | [`91e8bd8`](https://github.com/zinan92/trading-system/commit/91e8bd83d2f66fa41bd86a9e687e4fcd0614e617) | `issue-247-failure-copy-deploy-receipt` |
| #250 | [`05636e1`](https://github.com/zinan92/trading-system/commit/05636e17e922c2c2323e8987e19f58dc35ab73ea) | `issue-249-safe-recovery-receipts` |
| #252 | [`9a041cf`](https://github.com/zinan92/trading-system/commit/9a041cf973c7dab437b996e87fec60c507ea0497) | `issue-251-m1-05-deploy-receipt` |
| #254 | [`5d5b63b`](https://github.com/zinan92/trading-system/commit/5d5b63b5ffeaff89285ba1f28de2cdc9e24608bd) | `issue-253-dca-aggregate-tp-contract` |
| #256 | [`d4fce5c`](https://github.com/zinan92/trading-system/commit/d4fce5c3cff289ac8740915fd57dd5c584aa01a7) | `issue-255-m2-04-registry` |
| #258 | [`29227f7`](https://github.com/zinan92/trading-system/commit/29227f78c0640b8838bf7e5478fbe3c5c562c4ee) | `issue-257-dca-nautilus-reconciliation` |
| #260 | [`2cecbb5`](https://github.com/zinan92/trading-system/commit/2cecbb598855a221a9e17417fec95fb38bd6eee2) | `issue-259-registry-dca-reconciliation` |
| #262 | [`62a6e7a`](https://github.com/zinan92/trading-system/commit/62a6e7a2935e3f56041505bcbaf2af3f9624aa2c) | `issue-261-dca-risk-read-model` |
| #264 | [`1469976`](https://github.com/zinan92/trading-system/commit/1469976c2a8c0fea557d5bac2b570f8d8922ead4) | `issue-263-correct-dca-risk-root` |
| #276 | [`7933a84`](https://github.com/zinan92/trading-system/commit/7933a8453838e516c4341d34d3a19cde5fafe73f) | `issue-275-dashboard-read-model-payload` |
| #278 | [`569437a`](https://github.com/zinan92/trading-system/commit/569437ab784c69fe479bc2e4bc33e93f9e533835) | `issue-277-registry-dca-evidence` |
| #282 | [`14ce0f5`](https://github.com/zinan92/trading-system/commit/14ce0f502aaef888c6211691618bd17d93ed97c4) | `issue-281-grid-preview-after-dca` |
| #284 | [`6d7fea1`](https://github.com/zinan92/trading-system/commit/6d7fea1a3d7eb2cb19c1c43ca1b8cec3698dbc5a) | `issue-283-canonical-reconciliation-preflight` |
| #286 | [`6adf2c8`](https://github.com/zinan92/trading-system/commit/6adf2c8facb505d23cd06d457f993391cbff8f70) | `issue-285-registry-canonical-preflight` |
| #287 | [`d611873`](https://github.com/zinan92/trading-system/commit/d6118738c4e7bf30a99412fd16d1a2c6d19c48aa) | `issue-280-grid-lifecycle-package` |
| #290 | [`e883952`](https://github.com/zinan92/trading-system/commit/e883952d404ccdcdd8934e3659138ab2938b8bf6) | `issue-288-registry-grid-evidence` |
| #292 | [`bc4cfd8`](https://github.com/zinan92/trading-system/commit/bc4cfd82eebdd448be9e41186f0520d8afbf5180) | `issue-291-current-nautilus-accounting` |
| #294 | [`290f4f2`](https://github.com/zinan92/trading-system/commit/290f4f203d54da38ccf1e2b585a9a3f6fa2c4fa1) | `issue-293-dca-grid-switch-failsafe-stop` |
| #296 | [`bf8357d`](https://github.com/zinan92/trading-system/commit/bf8357d2cee38d0fe0bb76faf9d75372344a9390) | `issue-295-registry-recovery` |
| #297 | [`5df6046`](https://github.com/zinan92/trading-system/commit/5df6046bf8f004d318ea26ec845017c6be8b7365) | `issue-289-grid-rearm-evidence` |
| #299 | [`6e7ef0c`](https://github.com/zinan92/trading-system/commit/6e7ef0c28545282f7b8ee83d238cb31f20e2c1fb) | `issue-298-registry-grid-evidence` |
| #302 | [`e9f3591`](https://github.com/zinan92/trading-system/commit/e9f35915230797afce2c72a3470d7a958695d113) | `issue-301-grid-shadow-variants` |
| #304 | [`254bf37`](https://github.com/zinan92/trading-system/commit/254bf37c0e4491f4614b97c4dacd44c9d0315168) | `issue-303-registry-grid-shadows` |
| #306 | [`a05ba05`](https://github.com/zinan92/trading-system/commit/a05ba0598e8a2ec245167b13a2add3033533858e) | `issue-305-shadow-promotion-evidence` |
| #308 | [`6bbd58b`](https://github.com/zinan92/trading-system/commit/6bbd58b0850b47feb5dd1f7be8d672bd2155490c) | `issue-307-registry-shadow-gate` |
| #310 | [`db36902`](https://github.com/zinan92/trading-system/commit/db36902608df5ef38a928f3e9d17553d802f21b2) | `issue-309-shadow-promotion-proposal` |
| #312 | [`7f2ebb5`](https://github.com/zinan92/trading-system/commit/7f2ebb5ed1c9b97ca096115406505eeb24d1fe4c) | `issue-311-registry-shadow-proposal` |
| #314 | [`f90814c`](https://github.com/zinan92/trading-system/commit/f90814cef3771d9ad2631cbc613f3b50aa8f04ee) | `issue-313-safe-repair-queue` |
| #316 | [`3e86877`](https://github.com/zinan92/trading-system/commit/3e868771cb50fc5616224476a2c5241d007f2e62) | `issue-315-registry-safe-repair` |
| #318 | [`84a22f8`](https://github.com/zinan92/trading-system/commit/84a22f89c2f3e396807b8513917bbf5f2c240488) | `issue-317-parameter-draft-matrix` |
| #320 | [`b44199c`](https://github.com/zinan92/trading-system/commit/b44199ca437259acbf2683dec3eb490f87013603) | `issue-319-registry-m3-draft` |
| #322 | [`874ebad`](https://github.com/zinan92/trading-system/commit/874ebad7b72f4233674bc1cf0732b645b44d65c9) | `issue-321-range-drag-contract` |
| #324 | [`a29f278`](https://github.com/zinan92/trading-system/commit/a29f278a4698919632bcccf493adffc6d47886d5) | `issue-323-registry-m3-range-drag` |
| #326 | [`2129b13`](https://github.com/zinan92/trading-system/commit/2129b1368af99225278a050200d934d4a4019452) | `issue-325-start-stop-outcomes` |
| #328 | [`f982178`](https://github.com/zinan92/trading-system/commit/f982178d80a0c4ee6a4876f768dd91798a00fad1) | `issue-327-registry-m3-outcomes` |

## Prevention rule

Before a link is reported as delivered, the active GitHub account must be
`zinan92` and both objects must be read back:

```sh
gh auth switch -h github.com -u zinan92
gh api repos/zinan92/trading-system/issues/<issue-number> --jq '.html_url'
gh api repos/zinan92/trading-system/pulls/<pr-number> --jq '.html_url'
```

If either command returns 404, report the commit URL and the reachable
tracking Issue instead; do not claim the missing Issue/PR was created or
merged through GitHub metadata.
