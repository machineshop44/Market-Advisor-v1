package com.marketadvisor.companion

import android.net.http.SslCertificate
import java.io.ByteArrayInputStream
import java.security.MessageDigest
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate

/** Shared TLS pin normalize / SHA-256 fingerprint helpers. */
object TlsPin {
    fun normalize(fp: String): String =
        fp.trim().uppercase().replace(" ", "").replace("-", "")

    fun certFingerprint(cert: X509Certificate): String {
        val md = MessageDigest.getInstance("SHA-256")
        val dig = md.digest(cert.encoded)
        return dig.joinToString(":") { b -> "%02X".format(b) }
    }

    fun matches(expectedPin: String, gotFingerprint: String): Boolean {
        val expected = normalize(expectedPin)
        if (expected.isBlank()) return false
        return expected == normalize(gotFingerprint)
    }

    fun fingerprintFromSslCertificate(cert: SslCertificate?): String? {
        if (cert == null) return null
        return try {
            val bundle = SslCertificate.saveState(cert)
            val bytes = bundle.getByteArray("x509-certificate") ?: return null
            val x509 = CertificateFactory.getInstance("X.509")
                .generateCertificate(ByteArrayInputStream(bytes)) as X509Certificate
            certFingerprint(x509)
        } catch (_: Exception) {
            null
        }
    }
}
