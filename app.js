'use strict';
// Static product presentation. No research or provider API calls.
let toastTimer;
async function copy(text) {
  const toast = document.querySelector('#toast');
  try {
    await navigator.clipboard.writeText(text);
    toast.textContent = '已複製到剪貼簿';
  } catch {
    toast.textContent = '無法存取剪貼簿，請選取文字後複製。';
  }
  clearTimeout(toastTimer);
  toast.classList.add('show');
  toastTimer = setTimeout(() => toast.classList.remove('show'), 3500);
}
document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', () => copy(button.dataset.copy)));
document.querySelector('[data-copy-agent]').addEventListener('click', () => copy(document.querySelector('#agent-prompt').textContent));


const imageDialog = document.querySelector('#product-image-dialog');
const openImage = document.querySelector('#open-product-image');
openImage.addEventListener('click', () => imageDialog.showModal());
document.querySelector('#close-product-image').addEventListener('click', () => imageDialog.close());
imageDialog.addEventListener('click', event => {
  if (event.target === imageDialog) {
    const rect = imageDialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) imageDialog.close();
  }
});
imageDialog.addEventListener('close', () => openImage.focus());

const workflowDialog = document.querySelector('#workflow-diagram-dialog');
const openWorkflow = document.querySelector('#open-workflow-diagram');
openWorkflow.addEventListener('click', () => workflowDialog.showModal());
document.querySelector('#close-workflow-diagram').addEventListener('click', () => workflowDialog.close());
workflowDialog.addEventListener('click', event => {
  if (event.target === workflowDialog) {
    const rect = workflowDialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) workflowDialog.close();
  }
});
workflowDialog.addEventListener('close', () => openWorkflow.focus());
