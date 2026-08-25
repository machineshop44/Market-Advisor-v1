package com.marketadvisor.companion

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import com.marketadvisor.companion.databinding.ActivitySettingsBinding

class SettingsActivity : AppCompatActivity() {
    private lateinit var binding: ActivitySettingsBinding

    private val scanLauncher = registerForActivityResult(ScanContract()) { result ->
        val contents = result.contents ?: return@registerForActivityResult
        if (contents.isBlank()) {
            Toast.makeText(this, "No QR data", Toast.LENGTH_SHORT).show()
            return@registerForActivityResult
        }
        applySetupQr(contents)
    }

    private val cameraPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) {
            startQrScan()
        } else {
            Toast.makeText(this, "Camera permission required to scan setup QR", Toast.LENGTH_LONG).show()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.inputUrl.setText(Prefs.baseUrl(this))
        binding.inputUser.setText(Prefs.username(this))
        binding.inputPass.setText(Prefs.password(this))
        binding.inputFingerprint.setText(Prefs.fingerprint(this))

        binding.btnScanQr.setOnClickListener { ensureCameraAndScan() }
        binding.btnShareApk.setOnClickListener { ApkShareHelper.shareInstalledApk(this) }

        binding.btnSave.setOnClickListener {
            Prefs.save(
                this,
                binding.inputUrl.text?.toString().orEmpty(),
                binding.inputUser.text?.toString().orEmpty(),
                binding.inputPass.text?.toString().orEmpty(),
                binding.inputFingerprint.text?.toString().orEmpty(),
            )
            Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
            finish()
        }
    }

    private fun ensureCameraAndScan() {
        when {
            ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED -> startQrScan()
            else -> cameraPermissionLauncher.launch(Manifest.permission.CAMERA)
        }
    }

    private fun startQrScan() {
        val options = ScanOptions()
            .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
            .setPrompt("Scan Market Advisor setup QR")
            .setBeepEnabled(false)
            .setOrientationLocked(true)
            .setBarcodeImageEnabled(false)
        scanLauncher.launch(options)
    }

    private fun applySetupQr(raw: String) {
        val setup = try {
            CompanionSetupQr.parse(raw)
        } catch (e: Exception) {
            Toast.makeText(this, "Invalid setup QR", Toast.LENGTH_LONG).show()
            return
        }
        binding.inputUrl.setText(setup.url)
        binding.inputUser.setText(setup.user)
        binding.inputPass.setText(setup.pass)
        binding.inputFingerprint.setText(setup.fingerprint)
        Prefs.save(this, setup.url, setup.user, setup.pass, setup.fingerprint)
        Toast.makeText(this, "Setup imported — tap Save if you edit further", Toast.LENGTH_LONG).show()
    }
}
