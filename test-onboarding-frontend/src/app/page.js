'use client';

import React, { useState, useEffect, useRef } from 'react';
import Head from 'next/head';

export default function Home() {
  // Step tracker
  const [currentStep, setCurrentStep] = useState(0);

  // Global State
  const [plans, setPlans] = useState([]);
  const [selectedPlanId, setSelectedPlanId] = useState('');
  const [tempRegId, setTempRegId] = useState('');
  const [tempToken, setTempToken] = useState('');
  const [billingBreakdown, setBillingBreakdown] = useState(null);
  const [email, setEmail] = useState('');
  const [emailAvailable, setEmailAvailable] = useState(null);
  const [emailChecking, setEmailChecking] = useState(false);
  const [otpCode, setOtpCode] = useState('');
  const [otpSentTime, setOtpSentTime] = useState(null);
  const [resendCooldown, setResendCooldown] = useState(0);

  // User promotion states
  const [finalUser, setFinalUser] = useState(null);
  const [finalTokens, setFinalTokens] = useState(null);

  // Sign up Form Inputs
  const [signupForm, setSignupForm] = useState({
    first_name: 'John',
    last_name: 'Doe',
    business_name: 'JD Enterprises',
    password: 'SecurePassword123!',
    confirm_password: 'SecurePassword123!',
    referral_code: '',
  });

  // Billing Address & Phone Number Form Inputs
  const [billingForm, setBillingForm] = useState({
    phone_number: '919876543210',
    line1: 'Flat 304, IT Park Complex',
    city: 'Bengaluru',
    state: 'Karnataka',
    zip_code: '560001',
    country: 'IN',
    gstin: '',
  });

  // Forgot / Reset Password Inputs
  const [forgotEmail, setForgotEmail] = useState('');
  const [resetToken, setResetToken] = useState('');
  const [resetPasswordForm, setResetPasswordForm] = useState({
    password: 'NewSecurePassword123!',
    confirm_password: 'NewSecurePassword123!',
  });

  // Change Email Inputs
  const [newEmailInput, setNewEmailInput] = useState('');
  const [showChangeEmail, setShowChangeEmail] = useState(false);

  // Password visibility states
  const [showSignupPassword, setShowSignupPassword] = useState(false);
  const [showSignupConfirmPassword, setShowSignupConfirmPassword] = useState(false);

  // Debug Console Logs
  const [logs, setLogs] = useState([]);
  const logsEndRef = useRef(null);

  const addLog = (action, requestData, responseData, status) => {
    const newLog = {
      id: Date.now() + Math.random(),
      timestamp: new Date().toLocaleTimeString(),
      action,
      requestData,
      responseData,
      status,
    };
    setLogs((prev) => [...prev, newLog]);
  };

  useEffect(() => {
    if (logsEndRef.current) {
      logsEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs]);

  // Load plans on startup
  useEffect(() => {
    fetchPlans();
    loadRazorpayScript();
  }, []);

  // Cooldown countdown
  useEffect(() => {
    if (resendCooldown > 0) {
      const timer = setTimeout(() => setResendCooldown(resendCooldown - 1), 1000);
      return () => clearTimeout(timer);
    }
  }, [resendCooldown]);

  const loadRazorpayScript = () => {
    if (typeof window !== 'undefined' && !window.Razorpay) {
      const script = document.createElement('script');
      script.src = 'https://checkout.razorpay.com/v1/checkout.js';
      script.async = true;
      document.body.appendChild(script);
    }
  };

  const fetchPlans = async () => {
    try {
      const res = await fetch('http://localhost:8000/api/v1/onboard/plans/dropdown/');
      const data = await res.json();
      addLog('GET /plans/dropdown/', null, data, res.status);
      if (data.status && data.data) {
        setPlans(data.data);
        if (data.data.length > 0) {
          setSelectedPlanId(data.data[0].value);
        }
      }
    } catch (err) {
      addLog('GET /plans/dropdown/ Error', null, err.message, 'ERROR');
    }
  };

  // 1. Live Email Availability Checker
  const checkEmailUniqueness = async (emailToVerify) => {
    if (!emailToVerify || !emailToVerify.includes('@')) {
      setEmailAvailable(null);
      return;
    }
    setEmailChecking(true);
    try {
      const res = await fetch('http://localhost:8000/api/v1/onboard/check-email/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: emailToVerify }),
      });
      const data = await res.json();
      setEmailChecking(false);
      if (data.status) {
        setEmailAvailable(data.data.available);
        addLog('POST /check-email/', { email: emailToVerify }, data, res.status);
      } else {
        setEmailAvailable(false);
      }
    } catch (err) {
      setEmailChecking(false);
      addLog('POST /check-email/ Error', { email: emailToVerify }, err.message, 'ERROR');
    }
  };

  // 2. Submit Signup Form (Step 1: register-pending)
  const handleRegisterPending = async (e) => {
    e.preventDefault();
    const payload = {
      plan_id: selectedPlanId,
      email: email,
      ...signupForm,
    };
    try {
      const res = await fetch('http://localhost:8000/api/v1/onboard/register-pending/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      addLog('POST /register-pending/', payload, data, res.status);

      if (data.status && data.data) {
        setTempRegId(data.data.temp_registration_id);
        setOtpSentTime(new Date());
        setResendCooldown(60);
        setCurrentStep(1); // Proceed to OTP screen
      } else {
        alert(data.message || 'Registration failed');
      }
    } catch (err) {
      addLog('POST /register-pending/ Error', payload, err.message, 'ERROR');
    }
  };

  // 3. Submit OTP Code (Step 2: verify-otp)
  const handleVerifyOtp = async (e) => {
    e.preventDefault();
    const payload = {
      temp_registration_id: tempRegId,
      otp_code: otpCode,
    };
    try {
      const res = await fetch('http://localhost:8000/api/v1/onboard/verify-otp/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      addLog('POST /verify-otp/', payload, data, res.status);

      if (data.status && data.data) {
        setTempToken(data.data.temporary_token);
        setBillingBreakdown(data.data.billing_breakdown);
        setCurrentStep(2); // Proceed to pricing/checkout customizations
      } else {
        alert(data.message || 'Invalid OTP');
      }
    } catch (err) {
      addLog('POST /verify-otp/ Error', payload, err.message, 'ERROR');
    }
  };

  // 4. Change Email during OTP wait
  const handleChangeEmail = async (e) => {
    e.preventDefault();
    const payload = {
      temp_registration_id: tempRegId,
      new_email: newEmailInput,
    };
    try {
      const res = await fetch('http://localhost:8000/api/v1/onboard/change-email/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      addLog('POST /change-email/', payload, data, res.status);

      if (data.status) {
        setEmail(newEmailInput);
        setNewEmailInput('');
        setShowChangeEmail(false);
        setResendCooldown(60);
        alert('Email changed successfully. A new OTP has been sent!');
      } else {
        alert(data.message || 'Failed to change email');
      }
    } catch (err) {
      addLog('POST /change-email/ Error', payload, err.message, 'ERROR');
    }
  };

  // 5. Resend OTP
  const handleResendOtp = async () => {
    const payload = { temp_registration_id: tempRegId };
    try {
      const res = await fetch('http://localhost:8000/api/v1/onboard/resend-otp/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      addLog('POST /resend-otp/', payload, data, res.status);

      if (data.status) {
        setResendCooldown(60);
        alert('Verification OTP has been resent!');
      } else {
        alert(data.message || 'Cooldown or limit reached');
      }
    } catch (err) {
      addLog('POST /resend-otp/ Error', payload, err.message, 'ERROR');
    }
  };

  // 6. Update Selected Plan (Step 3: update-plan)
  const handleUpdatePlan = async (newPlanId) => {
    const payload = { plan_id: newPlanId };
    try {
      const res = await fetch('http://localhost:8000/api/v1/onboard/update-plan/', {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${tempToken}`,
        },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      addLog('PATCH /update-plan/', payload, data, res.status);

      if (data.status && data.data) {
        setSelectedPlanId(newPlanId);
        setBillingBreakdown(data.data.billing_breakdown);
      } else {
        alert(data.message || 'Failed to update plan');
      }
    } catch (err) {
      addLog('PATCH /update-plan/ Error', payload, err.message, 'ERROR');
    }
  };

  // 7. Request Password Reset Link
  const handleForgotPassword = async (e) => {
    e.preventDefault();
    const payload = { email: forgotEmail };
    try {
      const res = await fetch('http://localhost:8000/api/v1/onboard/forgot-password/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      addLog('POST /forgot-password/', payload, data, res.status);
      alert(data.message || 'Password reset requested.');
    } catch (err) {
      addLog('POST /forgot-password/ Error', payload, err.message, 'ERROR');
    }
  };

  // 8. Execute Password Reset using link token
  const handleResetPassword = async (e) => {
    e.preventDefault();
    const payload = {
      temp_registration_id: tempRegId,
      token: resetToken,
      ...resetPasswordForm,
    };
    try {
      const res = await fetch('http://localhost:8000/api/v1/onboard/reset-password/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      addLog('POST /reset-password/', payload, data, res.status);
      if (data.status) {
        alert('Password reset successfully!');
      } else {
        alert(data.message || 'Failed to reset password');
      }
    } catch (err) {
      addLog('POST /reset-password/ Error', payload, err.message, 'ERROR');
    }
  };

  // 9. Initiate JIT Checkout Order (Step 4: create-order-just-in-time)
  const handleCreateOrder = async (e) => {
    e.preventDefault();
    const payload = {
      phone_number: billingForm.phone_number,
      billing_address: {
        line1: billingForm.line1,
        city: billingForm.city,
        state: billingForm.state,
        zip_code: billingForm.zip_code,
        country: billingForm.country,
        gstin: billingForm.gstin || undefined,
      },
    };
    try {
      const res = await fetch('http://localhost:8000/api/v1/onboard/create-order-just-in-time/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${tempToken}`,
        },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      addLog('POST /create-order-just-in-time/', payload, data, res.status);

      if (data.status && data.data) {
        triggerRazorpayCheckout(data.data);
      } else {
        alert(data.message || 'Failed to create Razorpay Order');
      }
    } catch (err) {
      addLog('POST /create-order-just-in-time/ Error', payload, err.message, 'ERROR');
    }
  };

  // 10. Open Razorpay Checkout Modal
  const triggerRazorpayCheckout = (orderData) => {
    const options = {
      key: orderData.razorpay_key_id,
      amount: orderData.amount,
      currency: orderData.currency,
      name: 'Voicebot Subscriptions',
      description: 'Onboarding Membership activation',
      order_id: orderData.razorpay_order_id,
      handler: async function (response) {
        const verifyPayload = {
          razorpay_payment_id: response.razorpay_payment_id,
          razorpay_order_id: response.razorpay_order_id,
          razorpay_signature: response.razorpay_signature,
        };
        addLog('Razorpay Payment Success Callback', null, verifyPayload, 'SUCCESS');
        await handleVerifyPayment(verifyPayload);
      },
      prefill: {
        email: email,
        contact: billingForm.phone_number,
      },
      theme: {
        color: '#6366f1',
      },
      modal: {
        ondismiss: async function () {
          addLog('Razorpay Checkout Modal Dismissed', null, null, 'DISMISSED');
          // Call cancel order
          await handleCancelOrder(orderData.razorpay_order_id);
        },
      },
    };

    if (window.Razorpay) {
      const rzp = new window.Razorpay(options);
      rzp.open();
    } else {
      alert('Razorpay script is still loading. Please try again in a moment.');
    }
  };

  // 11. Verify Razorpay Signatures (Step 5: verify)
  const handleVerifyPayment = async (verifyPayload) => {
    try {
      const res = await fetch('http://localhost:8000/api/v1/onboard/verify/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${tempToken}`,
        },
        body: JSON.stringify(verifyPayload),
      });
      const data = await res.json();
      addLog('POST /verify/', verifyPayload, data, res.status);

      if (data.status && data.data) {
        setFinalTokens(data.data.tokens);
        setFinalUser(data.data.user);
        setCurrentStep(4); // Fully promoted!
      } else {
        alert(data.message || 'Payment signature verification failed.');
      }
    } catch (err) {
      addLog('POST /verify/ Error', verifyPayload, err.message, 'ERROR');
    }
  };

  // 12. Cancel Pending Order
  const handleCancelOrder = async (orderId) => {
    const payload = { razorpay_order_id: orderId };
    try {
      const res = await fetch('http://localhost:8000/api/v1/onboard/cancel-order/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${tempToken}`,
        },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      addLog('POST /cancel-order/', payload, data, res.status);
      alert('Order cancelled successfully.');
    } catch (err) {
      addLog('POST /cancel-order/ Error', payload, err.message, 'ERROR');
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans antialiased">
      {/* Header */}
      <header className="border-b border-slate-800 bg-slate-900/50 backdrop-blur px-8 py-5 flex items-center justify-between sticky top-0 z-50">
        <div className="flex items-center gap-3">
          <div className="h-10 w-10 rounded-xl bg-indigo-600 flex items-center justify-center font-bold text-white shadow-lg shadow-indigo-500/30">
            VB
          </div>
          <div>
            <h1 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
              Voicebot Onboarding
              <span className="text-xs px-2 py-0.5 rounded-full bg-indigo-500/20 text-indigo-400 border border-indigo-500/30 font-medium">
                Test Rig
              </span>
            </h1>
            <p className="text-xs text-slate-400">Step-by-Step Interactive Payments & Promotion Funnel</p>
          </div>
        </div>
        <div className="flex items-center gap-4">
          <button
            onClick={() => {
              setCurrentStep(0);
              setTempRegId('');
              setTempToken('');
              setFinalUser(null);
              setFinalTokens(null);
              setLogs([]);
            }}
            className="text-xs px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 font-medium transition duration-200 border border-slate-700"
          >
            Reset Funnel
          </button>
        </div>
      </header>

      {/* Main Content Area */}
      <main className="flex-grow w-full max-w-full mx-auto p-6 lg:p-8 grid grid-cols-1 lg:grid-cols-2 gap-8 items-start">
        {/* Left Side - Interactive Steps Form */}
        <section className="space-y-6">
          {/* Stepper Progress Bar */}
          <div className="mb-12">
            <div className="flex items-center justify-between max-w-xl">
              {[
                { label: 'Register', step: 0 },
                { label: 'OTP Code', step: 1 },
                { label: 'Select Plan', step: 2 },
                { label: 'Billing Address', step: 3 },
                { label: 'Success', step: 4 },
              ].map((s, idx) => (
                <React.Fragment key={idx}>
                  <div className="flex flex-col items-center">
                    <div
                      className={`h-9 w-9 rounded-full flex items-center justify-center font-semibold text-sm transition-all duration-300 border ${
                        currentStep === s.step
                          ? 'bg-indigo-600 border-indigo-500 text-white shadow-lg shadow-indigo-500/30'
                          : currentStep > s.step
                          ? 'bg-emerald-600 border-emerald-500 text-white'
                          : 'bg-slate-900 border-slate-800 text-slate-500'
                      }`}
                    >
                      {currentStep > s.step ? '✓' : idx + 1}
                    </div>
                    <span
                      className={`text-xs mt-2 font-medium ${
                        currentStep === s.step ? 'text-indigo-400' : 'text-slate-400'
                      }`}
                    >
                      {s.label}
                    </span>
                  </div>
                  {idx < 4 && (
                    <div
                      className={`flex-1 h-0.5 mx-4 -mt-6 transition-all duration-300 ${
                        currentStep > idx ? 'bg-emerald-600' : 'bg-slate-800'
                      }`}
                    />
                  )}
                </React.Fragment>
              ))}
            </div>
          </div>

          {/* Stepper Form Components */}
          <div className="bg-slate-900/60 border border-slate-800/80 rounded-2xl p-8 backdrop-blur shadow-xl relative overflow-hidden">
            {/* Step 0: Registration */}
            {currentStep === 0 && (
              <div>
                <h2 className="text-2xl font-bold text-white mb-2">Step 1: Create Your Account</h2>
                <p className="text-slate-400 text-sm mb-8">
                  Enter your details to initiate registration. We will send a secure verification code to your email.
                </p>

                <form onSubmit={handleRegisterPending} className="space-y-6">
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                        First Name
                      </label>
                      <input
                        type="text"
                        required
                        value={signupForm.first_name}
                        onChange={(e) => setSignupForm({ ...signupForm, first_name: e.target.value })}
                        className="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-3 text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                        Last Name
                      </label>
                      <input
                        type="text"
                        required
                        value={signupForm.last_name}
                        onChange={(e) => setSignupForm({ ...signupForm, last_name: e.target.value })}
                        className="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-3 text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition"
                      />
                    </div>
                  </div>

                  <div>
                    <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                      Business Name
                    </label>
                    <input
                      type="text"
                      required
                      value={signupForm.business_name}
                      onChange={(e) => setSignupForm({ ...signupForm, business_name: e.target.value })}
                      className="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-3 text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition"
                    />
                  </div>

                  <div>
                    <div className="flex justify-between items-center mb-2">
                      <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider">
                        Email Address
                      </label>
                      {emailChecking && <span className="text-xs text-indigo-400 animate-pulse">Checking uniqueness...</span>}
                      {emailAvailable === true && (
                        <span className="text-xs px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-400 border border-emerald-500/20 font-semibold">
                          ✓ Available
                        </span>
                      )}
                      {emailAvailable === false && (
                        <span className="text-xs px-2 py-0.5 rounded bg-rose-500/20 text-rose-400 border border-rose-500/20 font-semibold">
                          ✗ Taken / Active
                        </span>
                      )}
                    </div>
                    <input
                      type="email"
                      required
                      placeholder="e.g. user@domain.com"
                      value={email}
                      onChange={(e) => {
                        setEmail(e.target.value);
                        checkEmailUniqueness(e.target.value);
                      }}
                      className="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-3 text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition"
                    />
                  </div>

                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                        Password
                      </label>
                      <div className="relative flex items-center">
                        <input
                          type={showSignupPassword ? "text" : "password"}
                          required
                          value={signupForm.password}
                          onChange={(e) => setSignupForm({ ...signupForm, password: e.target.value })}
                          className="w-full bg-slate-950 border border-slate-800 rounded-lg pl-4 pr-10 py-3 text-slate-100 focus:outline-none focus:border-indigo-500 transition"
                        />
                        <button
                          type="button"
                          onClick={() => setShowSignupPassword(!showSignupPassword)}
                          className="absolute right-3 text-slate-500 hover:text-indigo-400 transition focus:outline-none"
                        >
                          {showSignupPassword ? (
                            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-5 h-5">
                              <path strokeLinecap="round" strokeLinejoin="round" d="M3.98 8.223A10.477 10.477 0 0 0 1.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.451 10.451 0 0 1 12 4.5c4.756 0 8.773 3.162 10.065 7.498a10.522 10.522 0 0 1-4.293 5.774M6.228 6.228 3 3m3.228 3.228 3.65 3.65m7.894 7.894L21 21m-3.228-3.228-3.65-3.65m0 0a3 3 0 1 0-4.243-4.243m4.242 4.242L9.88 9.88" />
                            </svg>
                          ) : (
                            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-5 h-5">
                              <path strokeLinecap="round" strokeLinejoin="round" d="M2.036 12.322a1.012 1.012 0 0 1 0-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178Z" />
                              <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" />
                            </svg>
                          )}
                        </button>
                      </div>
                    </div>
                    <div>
                      <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                        Confirm Password
                      </label>
                      <div className="relative flex items-center">
                        <input
                          type={showSignupConfirmPassword ? "text" : "password"}
                          required
                          value={signupForm.confirm_password}
                          onChange={(e) => setSignupForm({ ...signupForm, confirm_password: e.target.value })}
                          className="w-full bg-slate-950 border border-slate-800 rounded-lg pl-4 pr-10 py-3 text-slate-100 focus:outline-none focus:border-indigo-500 transition"
                        />
                        <button
                          type="button"
                          onClick={() => setShowSignupConfirmPassword(!showSignupConfirmPassword)}
                          className="absolute right-3 text-slate-500 hover:text-indigo-400 transition focus:outline-none"
                        >
                          {showSignupConfirmPassword ? (
                            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-5 h-5">
                              <path strokeLinecap="round" strokeLinejoin="round" d="M3.98 8.223A10.477 10.477 0 0 0 1.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.451 10.451 0 0 1 12 4.5c4.756 0 8.773 3.162 10.065 7.498a10.522 10.522 0 0 1-4.293 5.774M6.228 6.228 3 3m3.228 3.228 3.65 3.65m7.894 7.894L21 21m-3.228-3.228-3.65-3.65m0 0a3 3 0 1 0-4.243-4.243m4.242 4.242L9.88 9.88" />
                            </svg>
                          ) : (
                            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-5 h-5">
                              <path strokeLinecap="round" strokeLinejoin="round" d="M2.036 12.322a1.012 1.012 0 0 1 0-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178Z" />
                              <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" />
                            </svg>
                          )}
                        </button>
                      </div>
                    </div>
                  </div>

                  <div>
                    <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                      Select Plan (Initial)
                    </label>
                    <select
                      value={selectedPlanId}
                      onChange={(e) => setSelectedPlanId(e.target.value)}
                      className="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-3 text-slate-100 focus:outline-none focus:border-indigo-500 transition"
                    >
                      {plans.map((p) => (
                        <option key={p.value} value={p.value}>
                          {p.label}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div>
                    <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                      Referral Code (Optional)
                    </label>
                    <input
                      type="text"
                      placeholder="e.g. REF12345"
                      value={signupForm.referral_code}
                      onChange={(e) => setSignupForm({ ...signupForm, referral_code: e.target.value })}
                      className="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-3 text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition"
                    />
                  </div>

                  <button
                    type="submit"
                    className="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-bold py-4 rounded-xl transition duration-200 shadow-lg shadow-indigo-600/30"
                  >
                    Start Verification & SignUp
                  </button>
                </form>
              </div>
            )}

            {/* Step 1: OTP verification */}
            {currentStep === 1 && (
              <div>
                <h2 className="text-2xl font-bold text-white mb-2">Step 2: Enter Verification OTP</h2>
                <p className="text-slate-400 text-sm mb-6">
                  We sent a 5-digit security code to <strong className="text-indigo-400">{email}</strong>. Enter it below to unlock checkout.
                </p>

                <form onSubmit={handleVerifyOtp} className="space-y-6">
                  <div>
                    <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2 text-center">
                      5-Digit OTP Code
                    </label>
                    <input
                      type="text"
                      maxLength={5}
                      required
                      placeholder="e.g. 12345"
                      value={otpCode}
                      onChange={(e) => setOtpCode(e.target.value)}
                      className="w-full max-w-xs mx-auto text-center text-2xl font-bold bg-slate-950 border border-slate-800 rounded-lg px-4 py-4 tracking-widest text-indigo-400 focus:outline-none focus:border-indigo-500 transition"
                    />
                  </div>

                  <button
                    type="submit"
                    className="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-bold py-4 rounded-xl transition duration-200 shadow-lg shadow-indigo-600/30"
                  >
                    Verify Email & Proceed
                  </button>
                </form>

                <div className="mt-8 border-t border-slate-800/80 pt-6 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      disabled={resendCooldown > 0}
                      onClick={handleResendOtp}
                      className={`text-xs px-4 py-2 rounded-lg font-semibold transition ${
                        resendCooldown > 0
                          ? 'bg-slate-950 text-slate-600 border border-slate-800'
                          : 'bg-slate-800 hover:bg-slate-700 text-indigo-400 border border-slate-700'
                      }`}
                    >
                      Resend OTP {resendCooldown > 0 && `(${resendCooldown}s)`}
                    </button>
                  </div>

                  <div>
                    <button
                      type="button"
                      onClick={() => setShowChangeEmail(!showChangeEmail)}
                      className="text-xs text-indigo-400 hover:text-indigo-300 font-semibold"
                    >
                      Want to change email?
                    </button>
                  </div>
                </div>

                {/* Change Email Subform */}
                {showChangeEmail && (
                  <form onSubmit={handleChangeEmail} className="mt-6 p-6 bg-slate-950 rounded-xl border border-slate-800 space-y-4">
                    <h3 className="text-sm font-semibold text-white">Change Registration Email</h3>
                    <div>
                      <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                        New Email Address
                      </label>
                      <input
                        type="email"
                        required
                        placeholder="new.email@example.com"
                        value={newEmailInput}
                        onChange={(e) => setNewEmailInput(e.target.value)}
                        className="w-full bg-slate-900 border border-slate-800 rounded-lg px-4 py-2.5 text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition text-sm"
                      />
                    </div>
                    <button
                      type="submit"
                      className="w-full bg-slate-800 hover:bg-slate-700 text-white text-xs font-bold py-2.5 rounded-lg transition"
                    >
                      Change Email & Send New OTP
                    </button>
                  </form>
                )}
              </div>
            )}

            {/* Step 2: Plan Selection Customizations */}
            {currentStep === 2 && (
              <div>
                <h2 className="text-2xl font-bold text-white mb-2">Step 3: Select & Customize Plan</h2>
                <p className="text-slate-400 text-sm mb-8">
                  Review your current selected plan, or change it dynamically. The pricing, discounts, and taxes will immediately recalculate in real-time.
                </p>

                {/* Pricing Breakdown Card */}
                {billingBreakdown && (
                  <div className="bg-slate-950 p-6 rounded-xl border border-slate-800/80 mb-8 grid grid-cols-2 gap-4">
                    <div className="col-span-2 pb-2 border-b border-slate-800">
                      <span className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Active Plan Billing Details</span>
                    </div>
                    <div className="flex flex-col">
                      <span className="text-xs text-slate-500">Base Price</span>
                      <strong className="text-sm text-slate-300 font-semibold">{billingBreakdown.base_price} {billingBreakdown.currency}</strong>
                    </div>
                    <div className="flex flex-col">
                      <span className="text-xs text-slate-500">Discount ({billingBreakdown.discount_amount !== '0.00' ? '10%' : 'None'})</span>
                      <strong className="text-sm text-emerald-400 font-semibold">-{billingBreakdown.discount_amount} {billingBreakdown.currency}</strong>
                    </div>
                    <div className="flex flex-col">
                      <span className="text-xs text-slate-500">Net Price</span>
                      <strong className="text-sm text-slate-300 font-semibold">{billingBreakdown.net_base_price} {billingBreakdown.currency}</strong>
                    </div>
                    <div className="flex flex-col">
                      <span className="text-xs text-slate-500">Tax Amount ({billingBreakdown.tax_percentage}%)</span>
                      <strong className="text-sm text-slate-300 font-semibold">{billingBreakdown.tax_amount} {billingBreakdown.currency}</strong>
                    </div>
                    <div className="col-span-2 pt-2 border-t border-slate-800 flex justify-between items-center">
                      <span className="text-sm font-semibold text-white">Total Checkout Price</span>
                      <span className="text-xl font-bold text-indigo-400">{billingBreakdown.total_price} {billingBreakdown.currency}</span>
                    </div>
                  </div>
                )}

                <div className="space-y-6">
                  <div>
                    <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">
                      Select Another Plan to Update Instantly
                    </label>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      {plans.map((p) => (
                        <div
                          key={p.value}
                          onClick={() => handleUpdatePlan(p.value)}
                          className={`p-4 rounded-xl border-2 cursor-pointer transition flex flex-col justify-between ${
                            selectedPlanId === p.value
                              ? 'bg-indigo-950/30 border-indigo-500/80 shadow-lg shadow-indigo-500/10'
                              : 'bg-slate-950 border-slate-800 hover:border-slate-700'
                          }`}
                        >
                          <div className="flex justify-between items-center mb-1">
                            <span className="font-bold text-white text-sm">{p.label}</span>
                          </div>
                          <span className="text-xs text-indigo-400 font-medium mt-2">Click to select & update</span>
                        </div>
                      ))}
                    </div>
                  </div>

                  <button
                    onClick={() => setCurrentStep(3)}
                    className="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-bold py-4 rounded-xl transition duration-200 shadow-lg shadow-indigo-600/30"
                  >
                    Confirm Plan & Enter Billing Details
                  </button>
                </div>

                {/* Password Recovery Subsection */}
                <div className="mt-8 border-t border-slate-800/80 pt-6">
                  <h3 className="text-sm font-semibold text-white mb-2">Forgot Password during checkout?</h3>
                  <p className="text-xs text-slate-400 mb-4">You can trigger a secure recovery email below.</p>
                  <form onSubmit={handleForgotPassword} className="flex gap-4">
                    <input
                      type="email"
                      required
                      placeholder="Recovery email address"
                      value={forgotEmail}
                      onChange={(e) => setForgotEmail(e.target.value)}
                      className="flex-1 bg-slate-950 border border-slate-800 rounded-lg px-4 py-2 text-sm text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition"
                    />
                    <button
                      type="submit"
                      className="bg-slate-800 hover:bg-slate-700 text-indigo-400 border border-slate-700 font-semibold px-4 rounded-lg text-xs"
                    >
                      Send Reset Link
                    </button>
                  </form>
                </div>
              </div>
            )}

            {/* Step 3: Billing Address & JIT Checkout */}
            {currentStep === 3 && (
              <div>
                <h2 className="text-2xl font-bold text-white mb-2">Step 4: Enter Billing & Phone Number</h2>
                <p className="text-slate-400 text-sm mb-8">
                  Enter your address details and contact phone number. We will contact Razorpay to generate a local secure Transaction Order instantly.
                </p>

                <form onSubmit={handleCreateOrder} className="space-y-6">
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                        Phone Number (billing contact)
                      </label>
                      <input
                        type="text"
                        required
                        placeholder="e.g. 919876543210"
                        value={billingForm.phone_number}
                        onChange={(e) => setBillingForm({ ...billingForm, phone_number: e.target.value })}
                        className="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-3 text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition"
                      />
                    </div>

                    <div>
                      <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                        GSTIN (Optional Indian format)
                      </label>
                      <input
                        type="text"
                        placeholder="e.g. 29AAAAA1111A1Z1"
                        value={billingForm.gstin}
                        onChange={(e) => setBillingForm({ ...billingForm, gstin: e.target.value.toUpperCase() })}
                        className="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-3 text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition"
                      />
                    </div>
                  </div>

                  <div>
                    <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                      Billing Address Line 1
                    </label>
                    <input
                      type="text"
                      required
                      value={billingForm.line1}
                      onChange={(e) => setBillingForm({ ...billingForm, line1: e.target.value })}
                      className="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-3 text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition"
                    />
                  </div>

                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                        City
                      </label>
                      <input
                        type="text"
                        required
                        value={billingForm.city}
                        onChange={(e) => setBillingForm({ ...billingForm, city: e.target.value })}
                        className="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-3 text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                        State
                      </label>
                      <input
                        type="text"
                        required
                        value={billingForm.state}
                        onChange={(e) => setBillingForm({ ...billingForm, state: e.target.value })}
                        className="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-3 text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition"
                      />
                    </div>
                  </div>

                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                        ZIP / Postal Code
                      </label>
                      <input
                        type="text"
                        required
                        value={billingForm.zip_code}
                        onChange={(e) => setBillingForm({ ...billingForm, zip_code: e.target.value })}
                        className="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-3 text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                        Country (ISO 2-letter)
                      </label>
                      <input
                        type="text"
                        required
                        maxLength={2}
                        value={billingForm.country}
                        onChange={(e) => setBillingForm({ ...billingForm, country: e.target.value.toUpperCase() })}
                        className="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-3 text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition"
                      />
                    </div>
                  </div>

                  <button
                    type="submit"
                    className="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-bold py-4 rounded-xl transition duration-200 shadow-lg shadow-indigo-600/30 flex items-center justify-center gap-2"
                  >
                    🚀 Initiate Razorpay Order & Pay Now
                  </button>
                </form>
              </div>
            )}

            {/* Step 4: Fully Promoted Success */}
            {currentStep === 4 && (
              <div className="text-center py-6">
                <div className="h-16 w-16 bg-emerald-600/20 text-emerald-400 border border-emerald-500/30 rounded-full flex items-center justify-center text-3xl mx-auto mb-6 shadow-lg shadow-emerald-500/20">
                  ✓
                </div>
                <h2 className="text-2xl font-bold text-white mb-2">Onboarding Completed successfully!</h2>
                <p className="text-slate-400 text-sm mb-8 max-w-md mx-auto">
                  Your temporary registrant account has been promoted to a fully verified active tenant user mapping!
                </p>

                {finalUser && finalTokens && (
                  <div className="space-y-6 max-w-lg mx-auto text-left bg-slate-950 p-6 rounded-xl border border-slate-800/80">
                    <div>
                      <span className="block text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2">Promoted User Details</span>
                      <div className="grid grid-cols-2 gap-4 bg-slate-900/60 p-4 rounded-lg border border-slate-800/80 text-sm">
                        <span className="text-slate-500">User ID</span>
                        <strong className="text-slate-300 font-mono text-xs">{finalUser.id}</strong>
                        <span className="text-slate-500">Email Address</span>
                        <strong className="text-slate-300">{finalUser.email}</strong>
                        <span className="text-slate-500">Status</span>
                        <strong className="text-emerald-400">Active ✓</strong>
                      </div>
                    </div>

                    <div>
                      <span className="block text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2">Permanent Access JWTs</span>
                      <div className="space-y-2 font-mono text-xs text-indigo-400 overflow-x-auto bg-slate-900/60 p-4 rounded-lg border border-slate-800/80">
                        <div className="truncate">
                          <strong className="text-slate-400 block mb-1">Access Token:</strong>
                          {finalTokens.access}
                        </div>
                        <div className="truncate border-t border-slate-800 pt-2">
                          <strong className="text-slate-400 block mb-1">Refresh Token:</strong>
                          {finalTokens.refresh}
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                <button
                  onClick={() => {
                    setCurrentStep(0);
                    setTempRegId('');
                    setTempToken('');
                    setFinalUser(null);
                    setFinalTokens(null);
                  }}
                  className="mt-8 bg-indigo-600 hover:bg-indigo-500 text-white font-bold py-3 px-8 rounded-xl transition duration-200 shadow-lg shadow-indigo-600/30"
                >
                  Onboard Another User
                </button>
              </div>
            )}
          </div>
        </section>

        {/* Right Side - Real-Time API Logs Console */}
        <section className="border border-slate-800 bg-slate-900/40 backdrop-blur rounded-2xl flex flex-col h-[calc(100vh-10rem)] min-h-[600px] sticky top-24 overflow-hidden">
          <div className="p-5 border-b border-slate-800/80 flex items-center justify-between bg-slate-900/60">
            <span className="text-xs font-bold text-slate-400 uppercase tracking-wider flex items-center gap-2">
              <span className="h-2 w-2 rounded-full bg-emerald-500 animate-pulse" />
              API Logs Console
            </span>
            <span className="text-[10px] bg-slate-800 text-indigo-400 border border-slate-700/80 px-2 py-0.5 rounded font-mono font-medium">
              Live
            </span>
          </div>

          <div className="flex-1 overflow-y-auto p-5 font-mono text-xs space-y-4">
            {logs.length === 0 ? (
              <div className="text-slate-600 text-center py-12 italic">
                No API calls recorded yet. Complete steps in the funnel on the left.
              </div>
            ) : (
              logs.map((log) => (
                <div key={log.id} className="p-3.5 bg-slate-950 border border-slate-850 rounded-xl space-y-2 shadow">
                  <div className="flex justify-between items-center text-[10px] pb-1 border-b border-slate-900">
                    <span className="font-semibold text-slate-400">{log.action}</span>
                    <span className="text-slate-500">{log.timestamp}</span>
                  </div>

                  {log.requestData && (
                    <div>
                      <span className="text-[9px] text-slate-500 uppercase tracking-wider">Request Payload</span>
                      <pre className="mt-1 bg-slate-900 p-2 rounded text-[10px] text-indigo-300 overflow-x-auto max-h-32">
                        {JSON.stringify(log.requestData, null, 2)}
                      </pre>
                    </div>
                  )}

                  <div>
                    <div className="flex justify-between items-center mt-1">
                      <span className="text-[9px] text-slate-500 uppercase tracking-wider">Response JSON</span>
                      <span
                        className={`text-[9px] font-bold px-1.5 py-0.2 rounded ${
                          log.status === 200 || log.status === 201 || log.status === 'SUCCESS'
                            ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/10'
                            : 'bg-rose-500/20 text-rose-400 border border-rose-500/10'
                        }`}
                      >
                        HTTP {log.status}
                      </span>
                    </div>
                    <pre className="mt-1 bg-slate-900 p-2 rounded text-[10px] text-slate-300 overflow-x-auto max-h-48">
                      {JSON.stringify(log.responseData, null, 2)}
                    </pre>
                  </div>
                </div>
              ))
            )}
            <div ref={logsEndRef} />
          </div>
        </section>
      </main>
    </div>
  );
}
